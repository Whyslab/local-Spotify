/* Вариант 1 — «Обложка».
 *
 * Ось: изображение и глубина. Обложка играющего трека красит весь
 * интерфейс: акцент, фон «сейчас играет», выделение в списках. Вкладки и
 * мини-плеер — полупрозрачный материал, под которым видно прокрутку.
 * «Сейчас играет» — лист, который выезжает снизу и уходит вниз жестом.
 * Опора — Apple Music на айфоне. */
(function () {
    "use strict";
    const L = window.Lab;
    const { h, icon, clear } = L;

    let ctx = null;
    const els = {};
    let viewCleanup = null;
    let wide = false;

    // ------------------------------------------------------------------
    // Цвет из обложки
    // ------------------------------------------------------------------

    function toHsl([r, g, b]) {
        r /= 255; g /= 255; b /= 255;
        const max = Math.max(r, g, b), min = Math.min(r, g, b);
        let hue = 0, s = 0;
        const l = (max + min) / 2;
        if (max !== min) {
            const d = max - min;
            s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
            hue = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
            hue *= 60;
        }
        return [Math.round(hue), Math.round(s * 100), Math.round(l * 100)];
    }

    /* Акцент должен читаться на чёрном (контраст от 4,5), фон — оставаться
     * тёмным под белым текстом. Серая обложка даёт спокойный серый. */
    function applyTint(rgb) {
        const app = els.app;
        if (!app) return;
        if (!rgb) {
            app.style.setProperty("--tint", "hsl(350 92% 66%)");
            app.style.setProperty("--deep", "hsl(350 40% 16%)");
            app.style.setProperty("--mid", "hsl(330 45% 30%)");
            return;
        }
        const [hue, s] = toHsl(rgb);
        const sat = s < 12 ? s : Math.max(55, Math.min(90, s + 20));
        app.style.setProperty("--tint", `hsl(${hue} ${sat}% 68%)`);
        app.style.setProperty("--deep", `hsl(${hue} ${Math.min(sat, 55)}% 15%)`);
        app.style.setProperty("--mid", `hsl(${(hue + 18) % 360} ${Math.min(sat, 60)}% 30%)`);
    }

    // ------------------------------------------------------------------
    // Мелкие части
    // ------------------------------------------------------------------

    function btn(cls, iconName, label, onclick, extra) {
        return h("button", Object.assign({ class: cls, type: "button", "aria-label": label, title: label, onclick }, extra || {}),
            iconName ? icon(iconName) : null);
    }

    function capsule(label, iconName, onclick, cls) {
        return h("button", { class: "o-capsule " + (cls || ""), type: "button", onclick }, iconName ? icon(iconName) : null, h("span", null, label));
    }

    function bars() {
        return h("span", { class: "o-bars", "aria-hidden": "true" }, h("i"), h("i"), h("i"));
    }

    function isCurrent(track) {
        const c = L.current();
        return Boolean(c && track && c.path === track.path);
    }

    /* Строка трека. Главная кнопка включает список с этого места, «⋯» —
     * действия. Два соседних button, а не вложенные. */
    function trackRow(track, list, i, opts) {
        opts = opts || {};
        const row = h("div", { class: "o-row" + (isCurrent(track) ? " is-playing" : "") + (track.missing ? " is-missing" : ""), dataset: { path: track.path } },
            h("button", {
                class: "o-row-main", type: "button",
                onclick: () => L.play(list, i, { source: opts.source || "" }),
            },
                opts.number ? h("span", { class: "o-row-num" }, h("span", { class: "o-num" }, String(i + 1)), bars())
                    : h("span", { class: "o-row-art" }, L.cover(track.path, 48), bars()),
                h("span", { class: "o-row-text" },
                    h("span", { class: "o-row-title" }, track.title),
                    opts.hideArtist ? null : h("span", { class: "o-row-sub" }, track.artist || "")),
                h("span", { class: "o-row-time" }, L.fmtTime(track.duration))),
            btn("o-icon-btn o-row-more", "more", "Действия с треком", (e) => openMenu(track, e)));
        return row;
    }

    function markPlaying() {
        const c = L.current();
        for (const row of els.app.querySelectorAll(".o-row")) {
            row.classList.toggle("is-playing", Boolean(c && row.dataset.path === c.path));
        }
    }

    function section(title, action, ...body) {
        return h("section", { class: "o-section" },
            h("div", { class: "o-section-head" },
                h("h2", null, title),
                action ? h("button", { class: "o-link", type: "button", onclick: action.run }, action.label) : null),
            ...body);
    }

    function shelf(items, cls) {
        return h("div", { class: "o-shelf " + (cls || "") }, items);
    }

    function card(coverEl, title, sub, onclick, cls) {
        return h("button", { class: "o-card " + (cls || ""), type: "button", onclick },
            coverEl,
            h("span", { class: "o-card-title" }, title),
            sub ? h("span", { class: "o-card-sub" }, sub) : null);
    }

    function largeTitle(text, trailing) {
        return h("header", { class: "o-large" }, h("h1", null, text), trailing || null);
    }

    function backBar(label) {
        return h("div", { class: "o-backbar" },
            h("button", { class: "o-back", type: "button", onclick: () => history.back() }, icon("back"), h("span", null, label || "Назад")));
    }

    function empty(text, actionLabel, run) {
        return h("div", { class: "o-empty" }, h("p", null, text),
            actionLabel ? capsule(actionLabel, null, run, "is-tint") : null);
    }

    // ------------------------------------------------------------------
    // Экраны
    // ------------------------------------------------------------------

    function viewHome() {
        const d = L.data;
        const home = d.home || { moods: [], discover: { tracks: [] }, albums: [] };
        const moods = home.moods.map((m) => {
            const first = m.tracks[0];
            return h("div", { class: "o-mood" },
                h("button", { class: "o-mood-open", type: "button", onclick: () => L.go("mood", m.key), "aria-label": m.name },
                    first ? L.cover(first.path, 300) : h("div", { class: "cover is-missing" }),
                    h("span", { class: "o-mood-shade" }),
                    h("span", { class: "o-mood-text" },
                        h("span", { class: "o-mood-name" }, m.name),
                        h("span", { class: "o-mood-hint" }, m.hint))),
                btn("o-mood-play", "play", "Слушать «" + m.name + "»", () => L.play(m.tracks, 0, { source: m.name })));
        });

        const recent = d.tracks.slice(0, 18).map((t, i, list) =>
            card(L.cover(t.path, 160), t.title, t.artist, () => L.play(list, i, { source: "Недавно добавлено" })));

        const disc = (home.discover && home.discover.tracks) || [];
        const based = (home.discover && home.discover.based_on) || [];
        const picks = h("div", { class: "o-picks" }, disc.slice(0, 16).map((t, i) => trackRow(t, disc, i, { source: "Похоже на любимое" })));

        const albums = home.albums.map((a) => card(L.cover(a.cover, 160), a.album, a.artist,
            () => L.go("album", a.artist, a.album)));

        const topArtists = [...d.artists].sort((x, y) => y.tracks.length - x.tracks.length).slice(0, 14)
            .map((a) => card(L.cover(a.cover, 120, { class: "cover is-round" }), a.name, L.plural(a.tracks.length, ["трек", "трека", "треков"]),
                () => L.go("artist", a.name), "is-artist"));

        return h("div", { class: "o-page" },
            largeTitle("Главная", wide ? null : btn("o-icon-btn o-plus", "add", "Добавить музыку", () => L.go("add"))),
            section("Под настроение", null, shelf(moods, "is-moods")),
            disc.length ? section("Похоже на любимое", null,
                based.length ? h("p", { class: "o-section-note" }, "По тем, кого вы слушаете: " + based.slice(0, 3).join(", ")) : null,
                picks) : null,
            section("Недавно добавлено", { label: "Все", run: () => L.go("library", { tab: "tracks" }) }, shelf(recent)),
            albums.length ? section("Альбомы", { label: "Все", run: () => L.go("library", { tab: "albums" }) }, shelf(albums)) : null,
            section("Чаще всего в фонотеке", { label: "Все", run: () => L.go("library", { tab: "artists" }) }, shelf(topArtists, "is-artists")));
    }

    let librarySort = "new";
    function viewLibrary(r) {
        const tab = r.query.tab || "tracks";
        const seg = h("div", { class: "o-seg", role: "tablist" },
            [["tracks", "Треки"], ["artists", "Артисты"], ["albums", "Альбомы"]].map(([key, label]) =>
                h("button", {
                    class: "o-seg-item" + (tab === key ? " is-on" : ""), type: "button", role: "tab",
                    "aria-selected": tab === key ? "true" : "false",
                    onclick: () => L.go("library", { tab: key }),
                }, label)));

        let body;
        if (tab === "artists") {
            body = h("div", { class: "o-list" }, L.data.artists.map((a) =>
                h("button", { class: "o-artist-row", type: "button", onclick: () => L.go("artist", a.name) },
                    L.cover(a.cover, 48, { class: "cover is-round" }),
                    h("span", { class: "o-row-text" }, h("span", { class: "o-row-title" }, a.name),
                        h("span", { class: "o-row-sub" }, L.plural(a.tracks.length, ["трек", "трека", "треков"]))),
                    icon("back", "o-chev"))));
        } else if (tab === "albums") {
            const albums = L.data.albums.filter((a) => a.tracks.length > 1 || L.data.albums.length < 40);
            body = h("div", { class: "o-grid" }, albums.map((a) =>
                card(L.cover(a.cover, 220), a.album, a.artist, () => L.go("album", a.artist, a.album))));
        } else {
            const list = librarySort === "name"
                ? [...L.data.tracks].sort((x, y) => x.title.localeCompare(y.title, "ru", { sensitivity: "base" }))
                : L.data.tracks;
            body = h("div", null,
                h("div", { class: "o-actions" },
                    capsule("Слушать", "play", () => L.play(list, 0, { shuffle: false, source: "Фонотека" })),
                    capsule("Перемешать", "shuffle", () => L.play(list, -1, { shuffle: true, source: "Фонотека" }))),
                h("div", { class: "o-list-head" },
                    h("span", null, L.plural(list.length, ["трек", "трека", "треков"])),
                    h("button", {
                        class: "o-link", type: "button",
                        onclick: () => { librarySort = librarySort === "new" ? "name" : "new"; renderRoute(); },
                    }, librarySort === "new" ? "Сначала новые" : "По названию")),
                h("div", { class: "o-list" }, list.map((t, i) => trackRow(t, list, i, { source: "Фонотека" }))));
        }
        return h("div", { class: "o-page" },
            largeTitle("Фонотека", btn("o-icon-btn o-plus", "add", "Добавить музыку", () => L.go("add"))),
            seg, body);
    }

    /* Шапка коллекции — общая для альбома, подборки и настроения. */
    function collectionPage({ coverEl, title, subtitle, onSubtitle, meta, tracks, source, numbered, extra }) {
        const total = tracks.reduce((s, t) => s + (t.duration || 0), 0);
        return h("div", { class: "o-page o-collection" },
            backBar(),
            h("header", { class: "o-col-head" },
                h("div", { class: "o-col-art" }, coverEl),
                h("h1", { class: "o-col-title" }, title),
                subtitle ? h("button", { class: "o-col-sub", type: "button", onclick: onSubtitle || null, disabled: !onSubtitle }, subtitle) : null,
                h("p", { class: "o-col-meta" }, meta || (L.plural(tracks.length, ["трек", "трека", "треков"]) + ", " + L.fmtTotal(total))),
                h("div", { class: "o-actions is-center" },
                    capsule("Слушать", "play", () => L.play(tracks, 0, { shuffle: false, source })),
                    capsule("Перемешать", "shuffle", () => L.play(tracks, -1, { shuffle: true, source }))),
                extra || null),
            h("div", { class: "o-list" }, tracks.map((t, i) => trackRow(t, tracks, i, { source, number: numbered, hideArtist: false }))));
    }

    function viewAlbum(r) {
        const [artist, album] = r.params;
        const al = L.data.albumByKey.get(L.albumKey(artist, album));
        if (!al) return h("div", { class: "o-page" }, backBar(), empty("Такого альбома в фонотеке нет."));
        return collectionPage({
            coverEl: L.cover(al.cover, 300), title: al.album, subtitle: al.artist,
            onSubtitle: () => L.go("artist", al.artist), tracks: al.tracks, source: al.album, numbered: true,
        });
    }

    function viewMood(r) {
        const m = L.data.home && L.data.home.moods.find((x) => x.key === r.params[0]);
        if (!m) return h("div", { class: "o-page" }, backBar(), empty("Подборка по настроению обновилась — откройте главную."));
        const tracks = m.tracks.map((t) => L.data.byPath.get(t.path) || t);
        return collectionPage({
            coverEl: L.cover(m.tracks[0] && m.tracks[0].path, 300), title: m.name, subtitle: m.hint,
            tracks, source: m.name,
        });
    }

    function viewArtist(r) {
        const a = L.data.artistByName.get(r.params[0]);
        if (!a) return h("div", { class: "o-page" }, backBar(), empty("Такого артиста в фонотеке нет."));
        const albums = L.data.albums.filter((al) => al.artist === a.name);
        const page = h("div", { class: "o-page o-artist" },
            h("header", { class: "o-hero" },
                L.cover(a.cover, 600),
                h("div", { class: "o-hero-shade" }),
                h("button", { class: "o-back o-back-float", type: "button", onclick: () => history.back(), "aria-label": "Назад" }, icon("back")),
                h("div", { class: "o-hero-text" },
                    h("h1", null, a.name),
                    btn("o-play-round", "play", "Слушать " + a.name, () => L.play(a.tracks, 0, { source: a.name })))),
            h("div", { class: "o-actions" },
                capsule("Перемешать", "shuffle", () => L.play(a.tracks, -1, { shuffle: true, source: a.name })),
                h("span", { class: "o-muted" }, L.plural(a.tracks.length, ["трек", "трека", "треков"]))),
            section("Треки", null, h("div", { class: "o-list" }, a.tracks.map((t, i) => trackRow(t, a.tracks, i, { source: a.name, hideArtist: !t.artist.includes(" • ") })))),
            albums.length > 0 ? section("Альбомы", null, shelf(albums.map((al) =>
                card(L.cover(al.cover, 160), al.album, L.plural(al.tracks.length, ["трек", "трека", "треков"]), () => L.go("album", al.artist, al.album))))) : null);
        return page;
    }

    function viewPlaylists() {
        const pls = L.data.playlists;
        return h("div", { class: "o-page" },
            largeTitle("Подборки", btn("o-icon-btn o-plus", "add", "Новая подборка", () => L.act("Новая подборка"))),
            pls.length ? h("div", { class: "o-grid" }, pls.map((p) =>
                card(L.playlistCover(p, 220), p.name, L.plural(p.tracks, ["трек", "трека", "треков"]), () => L.go("playlist", p.name))))
                : empty("Подборок пока нет. Соберите первую из треков фонотеки.", "Новая подборка", () => L.act("Новая подборка")));
    }

    function viewPlaylist(r) {
        const name = r.params[0];
        const holder = h("div", { class: "o-page" }, backBar("Подборки"), h("div", { class: "o-skeleton" }));
        const meta = L.data.playlists.find((p) => p.name === name) || { name };
        L.playlistTracks(name).then((pl) => {
            if (!holder.isConnected) return;
            holder.replaceWith(collectionPage({
                coverEl: L.playlistCover(meta, 300), title: name,
                meta: L.plural(pl.tracks.length, ["трек", "трека", "треков"]) + ", " + L.fmtTotal(pl.tracks.reduce((s, t) => s + (t.duration || 0), 0)) +
                    (meta.updated_at ? ", изменена " + L.agoText(meta.updated_at) : ""),
                tracks: pl.tracks, source: name,
                extra: h("div", { class: "o-actions is-center is-quiet" },
                    h("button", { class: "o-link", type: "button", onclick: () => L.act("Изменить подборку") }, "Изменить"),
                    h("button", { class: "o-link", type: "button", onclick: () => L.act("Сменить обложку") }, "Обложка"),
                    h("button", { class: "o-link is-danger", type: "button", onclick: () => L.act("Удалить подборку") }, "Удалить")),
            }));
        }).catch((e) => { clear(holder, backBar(), empty(e.message)); });
        return holder;
    }

    let searchQuery = "";
    function viewSearch(r) {
        searchQuery = r.query.q !== undefined ? r.query.q : searchQuery;
        const results = h("div", { class: "o-results" });
        const input = h("input", {
            class: "o-search-input", type: "search", placeholder: "Артисты, треки, альбомы", value: searchQuery,
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
                const moods = (L.data.home ? L.data.home.moods : []).map((m) =>
                    h("button", { class: "o-browse", type: "button", onclick: () => L.go("mood", m.key) },
                        L.cover(m.tracks[0] && m.tracks[0].path, 160), h("span", null, m.name)));
                clear(results, section("Под настроение", null, h("div", { class: "o-browse-grid" }, moods)));
                return;
            }
            const found = L.searchLocal(q, 60);
            const yt = h("div", { class: "o-yt" },
                h("button", { class: "o-yt-ask", type: "button", onclick: () => askYouTube(q, yt) },
                    icon("search"), h("span", null, "Найти «" + q + "» на YouTube и добавить")));
            if (!found.tracks.length && !found.artists.length && !found.albums.length) {
                clear(results, empty("В фонотеке ничего не нашлось по «" + q + "»."), yt);
                return;
            }
            clear(results,
                found.artists.length ? section("Артисты", null, shelf(found.artists.map((a) =>
                    card(L.cover(a.cover, 120, { class: "cover is-round" }), a.name, null, () => L.go("artist", a.name), "is-artist")), "is-artists")) : null,
                found.albums.length ? section("Альбомы", null, shelf(found.albums.map((a) =>
                    card(L.cover(a.cover, 160), a.album, a.artist, () => L.go("album", a.artist, a.album))))) : null,
                found.tracks.length ? section("Треки", null, h("div", { class: "o-list" }, found.tracks.map((t, i) => trackRow(t, found.tracks, i, { source: "Поиск" })))) : null,
                yt);
        }

        fill();
        setTimeout(() => { if (!searchQuery) input.focus({ preventScroll: true }); }, 50);
        return h("div", { class: "o-page" },
            largeTitle("Поиск"),
            h("div", { class: "o-search" }, icon("search"), input),
            results);
    }

    function askYouTube(q, box) {
        clear(box, h("div", { class: "o-skeleton is-short" }));
        L.searchYouTube(q).then((body) => {
            const list = body.results || [];
            clear(box, section("На YouTube", null, list.length ? h("div", { class: "o-list" }, list.map((v) =>
                h("div", { class: "o-row" },
                    h("div", { class: "o-row-main is-static" },
                        h("span", { class: "o-row-art is-video" }, icon("track")),
                        h("span", { class: "o-row-text" },
                            h("span", { class: "o-row-title" }, v.title || ""),
                            h("span", { class: "o-row-sub" }, [v.channel || v.uploader || "", v.duration ? L.fmtTime(v.duration) : ""].filter(Boolean).join(", ")))),
                    h("button", { class: "o-capsule is-small", type: "button", onclick: () => L.act("Скачать в фонотеку") }, "Скачать"))))
                : empty("YouTube ничего не нашёл.")));
        }).catch((e) => clear(box, empty("Поиск на YouTube не ответил: " + e.message)));
    }

    function statusText(t) {
        return { done: "Готово", failed: "Не получилось", error: "Не получилось", processing: "Скачивается", downloading: "Скачивается", tagging: "Подписываем теги", queued: "В очереди", pending: "В очереди", skipped: "Уже есть" }[t.status] || t.status;
    }

    function viewAdd() {
        const area = h("textarea", {
            class: "o-textarea", rows: "4", placeholder: "Ссылки на YouTube, Spotify или Deezer — по одной в строке",
            "aria-label": "Ссылки для скачивания",
        });
        const tasksBox = h("div", { class: "o-list" }, h("div", { class: "o-skeleton" }));
        function loadTasks() {
            L.tasks().then((tasks) => {
                if (!tasksBox.isConnected) return;
                clear(tasksBox, tasks.slice(0, 30).map((t) =>
                    h("div", { class: "o-task is-" + t.status },
                        h("span", { class: "o-task-icon" }, icon(t.status === "done" ? "check" : /fail|error/.test(t.status) ? "warn" : "download")),
                        h("span", { class: "o-row-text" },
                            h("span", { class: "o-row-title" }, t.title || t.url),
                            h("span", { class: "o-row-sub" }, [t.artist, statusText(t), t.error].filter(Boolean).join(" — "))),
                        h("span", { class: "o-row-time" }, (t.updated_at || "").slice(5, 16).replace("T", " ")))));
            }).catch((e) => clear(tasksBox, empty(e.message)));
        }
        loadTasks();
        const timer = setInterval(loadTasks, 5000);
        viewCleanup = () => clearInterval(timer);
        return h("div", { class: "o-page" },
            wide ? largeTitle("Добавить музыку") : h("div", null, backBar(), largeTitle("Добавить музыку")),
            h("div", { class: "o-add" },
                area,
                h("div", { class: "o-actions" },
                    capsule("Скачать", "download", () => L.act("Скачать"), "is-tint"),
                    capsule("Из файлов", "add", () => L.act("Загрузить файлы")))),
            section("Загрузки", null, tasksBox));
    }

    function viewService() {
        const box = h("div", { class: "o-list o-inset" }, h("div", { class: "o-skeleton" }));
        L.health().then((s) => {
            const rows = [
                ["Служба", s.status === "healthy" ? "Работает" : s.status],
                ["Треков", String(s.tracks)], ["Альбомов", String(s.albums)], ["Подборок", String(s.playlists)],
                ["В очереди загрузки", String(s.queue_size)], ["Navidrome", s.navidrome === "configured" ? "Подключён" : s.navidrome],
                ["Ждут синхронизации", String(s.navidrome_pending)], ["Прослушиваний записано", String(s.plays_logged)],
                ["ffmpeg", s.ffmpeg], ["База", s.database],
            ];
            clear(box, rows.map(([k, v]) => h("div", { class: "o-kv" }, h("span", null, k), h("span", null, v))));
        }).catch((e) => clear(box, empty(e.message)));
        return h("div", { class: "o-page" }, largeTitle("Служба"), box,
            h("div", { class: "o-actions" }, capsule("Проверить фонотеку", "service", () => L.act("Проверить фонотеку")),
                capsule("Синхронизировать", "repeat", () => L.act("Синхронизировать с Navidrome"))));
    }

    const VIEWS = {
        home: viewHome, library: viewLibrary, artist: viewArtist, album: viewAlbum, mood: viewMood,
        playlists: viewPlaylists, playlist: viewPlaylist, search: viewSearch, add: viewAdd, service: viewService,
    };

    function renderRoute() {
        if (viewCleanup) { viewCleanup(); viewCleanup = null; }
        const r = L.route.current;
        updateNav(r);
        if (!L.data.ready) {
            clear(els.view, L.data.error
                ? h("div", { class: "o-page" }, empty(L.data.error.message, L.data.error.auth ? "Открыть плеер" : "Повторить",
                    () => { if (L.data.error.auth) location.href = "/"; else L.load(); }))
                : h("div", { class: "o-page" }, h("div", { class: "o-skeleton is-title" }), h("div", { class: "o-skeleton" }), h("div", { class: "o-skeleton" })));
            return;
        }
        const view = VIEWS[r.name] || VIEWS.home;
        els.view.replaceChildren(view(r));
        window.scrollTo(0, 0);
    }

    // ------------------------------------------------------------------
    // Навигация
    // ------------------------------------------------------------------

    const TABS = [
        ["home", "Главная", "home", ["home", "mood"]],
        ["library", "Фонотека", "library", ["library", "artist", "album", "add"]],
        ["playlists", "Подборки", "playlists", ["playlists", "playlist"]],
        ["search", "Поиск", "search", ["search"]],
    ];

    function updateNav(r) {
        const keys = [r.name];
        if (r.name === "library") keys.push("library-" + (r.query.tab || "tracks"));
        for (const b of els.app.querySelectorAll("[data-nav]")) {
            const on = b.dataset.nav.split(",").some((k) => keys.includes(k));
            b.classList.toggle("is-on", on);
            if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
        }
    }

    function buildSide() {
        const item = (route, label, iconName, match, params) =>
            h("button", { class: "o-side-item", type: "button", dataset: { nav: match.join(",") }, onclick: () => L.go(route, ...(params ? [params] : [])) },
                icon(iconName), h("span", null, label));
        const pls = h("div", { class: "o-side-pls" });
        const fillPls = () => clear(pls, L.data.playlists.map((p) =>
            h("button", { class: "o-side-item is-pl", type: "button", onclick: () => L.go("playlist", p.name) },
                L.playlistCover(p, 32), h("span", null, p.name))));
        fillPls();
        ctx.on("data", fillPls);
        const search = h("input", { class: "o-side-search", type: "search", placeholder: "Поиск", "aria-label": "Поиск" });
        search.addEventListener("keydown", (e) => { if (e.key === "Enter") L.go("search", { q: search.value }); });
        search.addEventListener("focus", () => { if (L.route.current.name !== "search") L.go("search", { q: search.value }); });
        return h("aside", { class: "o-side" },
            h("div", { class: "o-side-search-wrap" }, icon("search"), search),
            item("home", "Главная", "home", ["home", "mood"]),
            item("add", "Добавить", "add", ["add"]),
            h("p", { class: "o-side-label" }, "Фонотека"),
            item("library", "Треки", "track", ["library-tracks"], { tab: "tracks" }),
            item("library", "Артисты", "artist", ["library-artists", "artist"], { tab: "artists" }),
            item("library", "Альбомы", "album", ["library-albums", "album"], { tab: "albums" }),
            h("p", { class: "o-side-label" }, "Подборки"),
            item("playlists", "Все подборки", "grid", ["playlists"]),
            pls,
            h("div", { class: "o-side-foot" }, item("service", "Служба", "service", ["service"])));
    }

    // ------------------------------------------------------------------
    // Мини-плеер, полоса плеера, «сейчас играет»
    // ------------------------------------------------------------------

    function buildDock() {
        const art = h("div", { class: "o-mini-art" });
        const title = h("span", { class: "o-mini-title" });
        const sub = h("span", { class: "o-mini-sub" });
        const play = btn("o-icon-btn o-mini-play", "play", "Играть", () => L.toggle());
        const mini = h("div", { class: "o-mini", hidden: true },
            h("button", { class: "o-mini-open", type: "button", onclick: () => L.openNow(), "aria-label": "Открыть плеер" },
                art, h("span", { class: "o-mini-text" }, title, sub)),
            play, btn("o-icon-btn o-mini-next", "next", "Следующий", () => L.next()),
            h("div", { class: "o-mini-progress" }, h("i")));
        const tabs = h("nav", { class: "o-tabs", "aria-label": "Разделы" }, TABS.map(([route, label, iconName, match]) =>
            h("button", { class: "o-tab", type: "button", dataset: { nav: match.join(",") }, onclick: () => L.go(route) },
                icon(iconName), h("span", null, label))));
        Object.assign(els, { miniArt: art, miniTitle: title, miniSub: sub, miniPlay: play, mini });
        return h("div", { class: "o-dock" }, mini, tabs);
    }

    function buildBar() {
        const art = h("div", { class: "o-bar-art" });
        const title = h("span", { class: "o-bar-title" }, "Ничего не играет");
        const sub = h("span", { class: "o-bar-sub" }, "Выберите трек");
        const range = h("input", { class: "o-range o-bar-range", type: "range", min: "0", max: "1000", value: "0", step: "1", "aria-label": "Позиция в треке" });
        range.addEventListener("input", () => { range.dataset.drag = "1"; });
        range.addEventListener("change", () => { delete range.dataset.drag; L.seek(range.value / 1000 * L.duration()); });
        const t0 = h("span", { class: "o-bar-time" }, "0:00");
        const t1 = h("span", { class: "o-bar-time" }, "0:00");
        const play = btn("o-icon-btn o-bar-play", "play", "Играть", () => L.toggle());
        const shuf = btn("o-icon-btn o-toggle", "shuffle", "Перемешивать", () => L.setShuffle());
        const rep = btn("o-icon-btn o-toggle", "repeat", "Повтор", () => L.cycleRepeat());
        Object.assign(els, { barArt: art, barTitle: title, barSub: sub, barRange: range, barT0: t0, barT1: t1, barPlay: play, barShuf: shuf, barRep: rep });
        return h("header", { class: "o-bar" },
            h("div", { class: "o-bar-controls" }, shuf, btn("o-icon-btn", "prev", "Предыдущий", () => L.prev()), play,
                btn("o-icon-btn", "next", "Следующий", () => L.next()), rep),
            h("div", { class: "o-lcd" },
                h("button", { class: "o-lcd-open", type: "button", onclick: () => L.openNow(), "aria-label": "Открыть плеер" }, art),
                h("div", { class: "o-lcd-mid" },
                    h("div", { class: "o-lcd-text" }, title, sub),
                    h("div", { class: "o-lcd-progress" }, t0, range, t1))),
            h("div", { class: "o-bar-side" },
                btn("o-icon-btn", "lyrics", "Текст", () => L.openNow("lyrics")),
                btn("o-icon-btn", "queue", "Очередь", () => L.openNow("queue"))));
    }

    function buildNow() {
        const bg = h("div", { class: "o-now-bg" });
        els.nowBg = bg;
        const sheet = h("div", { class: "o-now", role: "dialog", "aria-modal": "true", "aria-label": "Сейчас играет", hidden: true },
            bg, h("div", { class: "o-now-inner" }));
        els.now = sheet;
        els.nowInner = sheet.lastChild;
        // Жест: тянуть вниз за верх листа — лист идёт за пальцем 1:1.
        let startY = 0, lastY = 0, lastT = 0, velocity = 0, dragging = false;
        sheet.addEventListener("pointerdown", (e) => {
            if (wide || !e.target.closest(".o-now-grab, .o-now-art, .o-now-head")) return;
            dragging = true; startY = lastY = e.clientY; lastT = e.timeStamp; velocity = 0;
            sheet.classList.add("is-dragging");
            sheet.setPointerCapture(e.pointerId);
        });
        sheet.addEventListener("pointermove", (e) => {
            if (!dragging) return;
            const dy = Math.max(0, e.clientY - startY);
            const dt = Math.max(1, e.timeStamp - lastT);
            velocity = (e.clientY - lastY) / dt;
            lastY = e.clientY; lastT = e.timeStamp;
            // За верхний край — с сопротивлением, а не жёстким упором.
            sheet.style.transform = `translateY(${dy}px)`;
        });
        const release = () => {
            if (!dragging) return;
            dragging = false;
            sheet.classList.remove("is-dragging");
            const dy = lastY - startY;
            sheet.style.transform = "";
            if (dy > 140 || velocity > 0.6) L.closeNow();
        };
        sheet.addEventListener("pointerup", release);
        sheet.addEventListener("pointercancel", release);
        return sheet;
    }

    function renderNow() {
        const t = L.current();
        const open = L.ui.now && Boolean(t);
        const sheet = els.now;
        if (!open) {
            if (!sheet.hidden) {
                sheet.classList.remove("is-open");
                document.documentElement.classList.remove("o-locked");
                setTimeout(() => { if (!L.ui.now) sheet.hidden = true; }, 420);
            }
            return;
        }
        const tab = L.ui.nowTab;
        const range = h("input", { class: "o-range o-now-range", type: "range", min: "0", max: "1000", value: "0", step: "1", "aria-label": "Позиция в треке" });
        range.addEventListener("input", () => { range.dataset.drag = "1"; els.nowT0.textContent = L.fmtTime(range.value / 1000 * L.duration()); });
        range.addEventListener("change", () => { delete range.dataset.drag; L.seek(range.value / 1000 * L.duration()); });
        const t0 = h("span", null, "0:00");
        const t1 = h("span", null, "0:00");
        const play = btn("o-now-play", L.player.audio.paused ? "play" : "pause", "Играть или пауза", () => L.toggle());
        Object.assign(els, { nowRange: range, nowT0: t0, nowT1: t1, nowPlay: play });

        const head = h("div", { class: "o-now-head" },
            h("div", { class: "o-now-small" }, L.cover(t.path, 96)),
            h("div", { class: "o-now-text" },
                h("div", { class: "o-now-title" }, t.title),
                h("button", { class: "o-now-artist", type: "button", onclick: () => { L.closeNow(); L.go("artist", L.mainArtist(t)); } }, t.artist || "")),
            btn("o-icon-btn o-now-more", "more", "Действия", (e) => openMenu(t, e)));

        const art = h("div", { class: "o-now-art" + (L.player.audio.paused ? " is-paused" : "") }, L.cover(t.path, 600));
        els.nowArt = art;
        const controls = h("div", { class: "o-now-controls" },
            h("div", { class: "o-now-progress" }, range, h("div", { class: "o-now-times" }, t0, t1)),
            h("div", { class: "o-now-buttons" },
                btn("o-icon-btn o-toggle" + (L.player.shuffle ? " is-on" : ""), "shuffle", "Перемешивать", () => L.setShuffle()),
                btn("o-now-skip", "prev", "Предыдущий", () => L.prev()),
                play,
                btn("o-now-skip", "next", "Следующий", () => L.next()),
                btn("o-icon-btn o-toggle" + (L.player.repeat !== "off" ? " is-on" : "") + (L.player.repeat === "one" ? " is-one" : ""), "repeat",
                    L.player.repeat === "one" ? "Повтор трека" : "Повтор", () => L.cycleRepeat())),
            h("div", { class: "o-now-tabs" },
                btn("o-icon-btn" + (tab === "lyrics" ? " is-on" : ""), "lyrics", "Текст", () => L.setNowTab(tab === "lyrics" ? "cover" : "lyrics")),
                h("span", { class: "o-now-source" }, L.player.source ? "Из: " + L.player.source : ""),
                btn("o-icon-btn" + (tab === "queue" ? " is-on" : ""), "queue", "Очередь", () => L.setNowTab(tab === "queue" ? "cover" : "queue"))));

        let side = null;
        if (tab === "lyrics") side = lyricsPanel(t);
        else if (tab === "queue") side = queuePanel();

        clear(els.nowInner,
            h("button", { class: "o-now-grab", type: "button", onclick: () => L.closeNow(), "aria-label": "Свернуть" }, wide ? icon("down") : h("i")),
            h("div", { class: "o-now-body is-" + tab },
                h("div", { class: "o-now-main" }, art, head, controls),
                side ? h("div", { class: "o-now-side" }, side) : null));
        if (els.nowBg.dataset.path !== t.path) {
            els.nowBg.dataset.path = t.path;
            clear(els.nowBg, L.cover(t.path, 96));
        }
        L.coverColor(t.path).then((rgb) => { if (L.current() === t) applyTint(rgb); });
        onTime();

        if (sheet.hidden) {
            sheet.hidden = false;
            document.documentElement.classList.add("o-locked");
            requestAnimationFrame(() => requestAnimationFrame(() => sheet.classList.add("is-open")));
        }
    }

    function lyricsPanel(t) {
        const box = h("div", { class: "o-lyrics", "aria-live": "off" }, h("p", { class: "o-lyrics-wait" }, "Ищем текст…"));
        els.lyrics = box;
        els.lyricLines = null;
        L.lyrics(t.path).then((body) => {
            if (L.current() !== t || !box.isConnected) return;
            if (body.synced && body.synced.length) {
                const lines = body.synced.map((s) => h("button", {
                    class: "o-line", type: "button", dataset: { at: String(s.at) },
                    onclick: () => L.seek(s.at),
                }, s.line || "♪"));
                els.lyricLines = lines;
                clear(box, lines);
                onTime();
            } else if (body.plain) {
                clear(box, body.plain.split("\n").map((line) => h("p", { class: "o-line is-plain" }, line || " ")));
            } else {
                clear(box, h("p", { class: "o-lyrics-wait" }, body.reason || "Текста для этого трека нет."),
                    capsule("Найти другой текст", "search", () => L.act("Выбрать текст")));
            }
        });
        return box;
    }

    function queuePanel() {
        const next = L.upcoming();
        return h("div", { class: "o-queue" },
            h("div", { class: "o-queue-head" }, h("h3", null, "Далее"),
                h("span", { class: "o-muted" }, next.length ? L.plural(next.length, ["трек", "трека", "треков"]) : "")),
            next.length ? h("div", { class: "o-list" }, next.slice(0, 80).map(({ track, index }) =>
                h("div", { class: "o-row" },
                    h("button", { class: "o-row-main", type: "button", onclick: () => L.playAt(index) },
                        h("span", { class: "o-row-art" }, L.cover(track.path, 48)),
                        h("span", { class: "o-row-text" }, h("span", { class: "o-row-title" }, track.title), h("span", { class: "o-row-sub" }, track.artist || "")),
                        h("span", { class: "o-row-time" }, L.fmtTime(track.duration))))))
                : h("p", { class: "o-muted" }, "Очередь закончится на этом треке."));
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
            els.barT0.textContent = L.fmtTime(pos);
            els.barT1.textContent = "−" + L.fmtTime(Math.max(0, dur - pos));
        }
        if (els.nowRange && els.nowRange.isConnected && !els.nowRange.dataset.drag) {
            els.nowRange.value = String(Math.round(frac * 1000));
            els.nowRange.style.setProperty("--p", String(frac));
            els.nowT0.textContent = L.fmtTime(pos);
            els.nowT1.textContent = "−" + L.fmtTime(Math.max(0, dur - pos));
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
        L.coverColor(t.path).then((rgb) => { if (L.current() === t) applyTint(rgb); });
        markPlaying();
        if (L.ui.now) renderNow();
    }

    function onState() {
        const paused = L.player.audio.paused;
        const swap = (b) => { if (b) clear(b, icon(paused ? "play" : "pause")); };
        swap(els.miniPlay); swap(els.barPlay); swap(els.nowPlay);
        for (const b of [els.miniPlay, els.barPlay, els.nowPlay]) if (b) b.setAttribute("aria-label", paused ? "Играть" : "Пауза");
        if (els.nowArt) els.nowArt.classList.toggle("is-paused", paused);
        els.app.classList.toggle("is-paused", paused);
        els.app.classList.toggle("is-loading", L.player.loading);
        if (els.barShuf) els.barShuf.classList.toggle("is-on", L.player.shuffle);
        if (els.barRep) {
            els.barRep.classList.toggle("is-on", L.player.repeat !== "off");
            els.barRep.classList.toggle("is-one", L.player.repeat === "one");
        }
        if (L.ui.now && els.now && !els.now.hidden) {
            const tog = els.now.querySelectorAll(".o-now-buttons .o-toggle");
            if (tog[0]) tog[0].classList.toggle("is-on", L.player.shuffle);
            if (tog[1]) {
                tog[1].classList.toggle("is-on", L.player.repeat !== "off");
                tog[1].classList.toggle("is-one", L.player.repeat === "one");
            }
        }
    }

    // ------------------------------------------------------------------
    // Меню действий: лист снизу на телефоне, меню у курсора на компьютере
    // ------------------------------------------------------------------

    function openMenu(track, event) {
        closeMenu();
        const actions = L.trackActions(track);
        const list = h("div", { class: "o-menu-list", role: "menu" }, actions.map((a) =>
            h("button", { class: "o-menu-item" + (a.danger ? " is-danger" : ""), type: "button", role: "menuitem", onclick: () => { closeMenu(); a.run(); } },
                h("span", null, a.label), icon(a.icon))));
        const head = h("div", { class: "o-menu-head" }, L.cover(track.path, 48),
            h("span", { class: "o-row-text" }, h("span", { class: "o-row-title" }, track.title), h("span", { class: "o-row-sub" }, track.artist || "")));
        const menu = h("div", { class: "o-menu" + (wide ? " is-pop" : " is-sheet") }, wide ? null : head, list,
            wide ? null : h("button", { class: "o-menu-cancel", type: "button", onclick: closeMenu }, "Отменить"));
        const scrim = h("div", { class: "o-scrim", onclick: closeMenu });
        els.menu = h("div", { class: "o-menu-layer" }, scrim, menu);
        els.app.appendChild(els.menu);
        if (wide && event) {
            const rect = menu.getBoundingClientRect();
            const x = Math.min(event.clientX, innerWidth - rect.width - 12);
            const y = Math.min(event.clientY, innerHeight - rect.height - 12);
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
        }
    }

    // ------------------------------------------------------------------

    function mount(c) {
        ctx = c;
        const mq = matchMedia("(min-width: 900px)");
        wide = mq.matches;
        const onMq = () => { wide = mq.matches; renderRoute(); if (L.ui.now) renderNow(); };
        mq.addEventListener("change", onMq);
        ctx.cleanup(() => mq.removeEventListener("change", onMq));
        document.addEventListener("keydown", onKey);
        ctx.cleanup(() => document.removeEventListener("keydown", onKey));
        ctx.cleanup(() => document.documentElement.classList.remove("o-locked"));

        els.view = h("div", { class: "o-view" });
        els.app = h("div", { class: "o-app" });
        applyTint(null);
        clear(els.app, buildSide(), h("main", { class: "o-main" }, buildBar(), els.view), buildDock(), buildNow());
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

    L.register({ id: "v1", name: "Обложка", themeColor: "#000000", mount, unmount });
})();
