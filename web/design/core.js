/* Design lab core — общее для четырёх вариантов.
 *
 * Варианты различаются раскладкой и оформлением, но играют одну и ту же
 * музыку из одной фонотеки. Поэтому всё, что не про внешний вид, живёт
 * здесь: данные, обложки, плеер, адреса экранов, переключатель. Вариант
 * получает это через window.Lab и только рисует.
 *
 * Только чтение. Отсюда уходят одни GET-запросы; всё, что в настоящем
 * плеере что-то меняет (удалить, скачать, править подборку), здесь
 * вызывает Lab.act() — подсказку, что в макете кнопка ничего не делает.
 *
 * Всё, что пришло с сервера (названия из YouTube, теги), вставляется
 * только через textContent: токен лежит в localStorage этого же адреса,
 * и одна вставка через innerHTML отдала бы его чужому названию трека. */
(function () {
    "use strict";

    // ------------------------------------------------------------------
    // DOM
    // ------------------------------------------------------------------

    /* h("div", {class: "x", onclick: fn}, "text", child, [children]) —
     * строки становятся текстовыми узлами, никогда разметкой. */
    function h(tag, attrs, ...kids) {
        const el = document.createElement(tag);
        if (attrs) {
            for (const [key, value] of Object.entries(attrs)) {
                if (value === null || value === undefined || value === false) continue;
                if (key === "class") el.className = value;
                else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
                else if (key === "dataset") Object.assign(el.dataset, value);
                else if (key.startsWith("on")) {
                    // Обработчик — только функция: строка из данных не должна
                    // стать атрибутом onclick.
                    if (typeof value === "function") el.addEventListener(key.slice(2), value);
                } else if (key === "text") el.textContent = value;
                else if (value === true) el.setAttribute(key, "");
                else el.setAttribute(key, String(value));
            }
        }
        append(el, kids);
        return el;
    }

    function append(el, kids) {
        for (const kid of kids) {
            if (kid === null || kid === undefined || kid === false) continue;
            if (Array.isArray(kid)) append(el, kid);
            else if (kid instanceof Node) el.appendChild(kid);
            else el.appendChild(document.createTextNode(String(kid)));
        }
    }

    function clear(el, ...kids) {
        el.replaceChildren();
        append(el, kids);
        return el;
    }

    /* Иконки — один набор, нарисованный одной линией. Вариант задаёт
     * толщину и заливку через CSS (stroke-width, fill на .ic-*). */
    const ICONS = {
        play: '<path d="M7 4.5v15l12.5-7.5z" data-fill="1"/>',
        pause: '<rect x="6" y="4.5" width="4" height="15" rx="1" data-fill="1"/><rect x="14" y="4.5" width="4" height="15" rx="1" data-fill="1"/>',
        next: '<path d="M5 5v14l10-7z" data-fill="1"/><path d="M18.5 5v14"/>',
        prev: '<path d="M19 5v14L9 12z" data-fill="1"/><path d="M5.5 5v14"/>',
        shuffle: '<path d="M3 7h3.5c4.5 0 6.5 10 11 10H21"/><path d="M3 17h3.5c1.8 0 3-1.6 4.1-3.6M13.4 9.6C14.5 8 15.7 7 17.5 7H21"/><path d="m18 4 3 3-3 3M18 14l3 3-3 3"/>',
        repeat: '<path d="M4 11V9a3 3 0 0 1 3-3h13"/><path d="m17 3 3 3-3 3"/><path d="M20 13v2a3 3 0 0 1-3 3H4"/><path d="m7 21-3-3 3-3"/>',
        home: '<path d="M4 10.5 12 4l8 6.5V20h-5.5v-6h-5v6H4z"/>',
        library: '<path d="M5 4v16M9.5 4v16"/><path d="m14 4.6 4.6 15"/>',
        playlists: '<path d="M4 6h11M4 11h11M4 16h7"/><circle cx="17" cy="17" r="2.6"/><path d="M19.6 17V7.5l2.4-1"/>',
        search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
        add: '<path d="M12 5v14M5 12h14"/>',
        download: '<path d="M12 4v11"/><path d="m7 10.5 5 5 5-5"/><path d="M5 20h14"/>',
        lyrics: '<path d="M5 6.5h14M5 11h14M5 15.5h9"/><path d="M5 20h6"/>',
        queue: '<path d="M4 6h12M4 11h12M4 16h7"/><path d="m15 14 5 3-5 3z" data-fill="1"/>',
        more: '<circle cx="5.5" cy="12" r="1.6" data-fill="1"/><circle cx="12" cy="12" r="1.6" data-fill="1"/><circle cx="18.5" cy="12" r="1.6" data-fill="1"/>',
        back: '<path d="m14.5 5-7 7 7 7"/>',
        close: '<path d="M6 6l12 12M18 6 6 18"/>',
        down: '<path d="m5.5 9 6.5 6.5L18.5 9"/>',
        heart: '<path d="M12 20s-7.5-4.6-7.5-10.2A4.3 4.3 0 0 1 12 7.3a4.3 4.3 0 0 1 7.5 2.5C19.5 15.4 12 20 12 20z"/>',
        service: '<path d="M4 13h3.5l2-6 4 11 2-5H20"/>',
        artist: '<circle cx="12" cy="8.5" r="3.8"/><path d="M4.5 20c1.2-4 4-5.8 7.5-5.8s6.3 1.8 7.5 5.8"/>',
        album: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2.2"/>',
        track: '<path d="M9 18V6l10-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="16.5" cy="16" r="2.5"/>',
        clock: '<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 2"/>',
        spark: '<path d="M12 3.5 13.8 10l6.7 2-6.7 2L12 20.5 10.2 14l-6.7-2 6.7-2z"/>',
        check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
        warn: '<path d="M12 4 21 19.5H3z"/><path d="M12 10v4.5M12 17.2v.3"/>',
        trash: '<path d="M5 7h14M10 7V4.8h4V7M7 7l1 13h8l1-13"/>',
        link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
        volume: '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z" data-fill="1"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
        sort: '<path d="M7 5v14M4 16l3 3 3-3"/><path d="M14 6h6M14 11h4.5M14 16h3"/>',
        grid: '<rect x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5"/>',
        list: '<path d="M8.5 6H20M8.5 12H20M8.5 18H20"/><path d="M4 6h.5M4 12h.5M4 18h.5"/>',
        radio: '<circle cx="12" cy="12" r="2"/><path d="M8 8a5.6 5.6 0 0 0 0 8M16 8a5.6 5.6 0 0 1 0 8M5.2 5.2a9.6 9.6 0 0 0 0 13.6M18.8 5.2a9.6 9.6 0 0 1 0 13.6"/>',
        menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    };
    const SVG_NS = "http://www.w3.org/2000/svg";
    /* Разметка иконок — своя, из этого файла, не с сервера. DOMParser, а
     * не innerHTML: так правило «никакого innerHTML» держится без исключений. */
    const parsedIcons = {};
    function icon(name, cls) {
        if (!parsedIcons[name]) {
            const doc = new DOMParser().parseFromString(
                `<svg xmlns="${SVG_NS}">${ICONS[name] || ""}</svg>`, "image/svg+xml");
            parsedIcons[name] = doc.documentElement;
        }
        const svg = document.createElementNS(SVG_NS, "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("aria-hidden", "true");
        svg.setAttribute("class", "ic ic-" + name + (cls ? " " + cls : ""));
        for (const node of parsedIcons[name].childNodes) svg.appendChild(document.importNode(node, true));
        return svg;
    }

    // ------------------------------------------------------------------
    // Форматирование
    // ------------------------------------------------------------------

    function fmtTime(sec) {
        if (!Number.isFinite(sec) || sec < 0) sec = 0;
        const m = Math.floor(sec / 60);
        const s = Math.floor(sec % 60);
        return m + ":" + String(s).padStart(2, "0");
    }

    function fmtTotal(sec) {
        const minutes = Math.round(sec / 60);
        if (minutes < 60) return minutes + " мин";
        const hours = Math.floor(minutes / 60);
        const rest = minutes % 60;
        return hours + " ч" + (rest ? " " + rest + " мин" : "");
    }

    /* plural(5, ["трек", "трека", "треков"]) → "5 треков" */
    function plural(n, forms) {
        const a = Math.abs(n) % 100;
        const b = a % 10;
        const form = a > 10 && a < 20 ? forms[2] : b > 1 && b < 5 ? forms[1] : b === 1 ? forms[0] : forms[2];
        return n + " " + form;
    }

    function agoText(unix) {
        if (!unix) return "";
        const days = Math.floor((Date.now() / 1000 - unix) / 86400);
        if (days <= 0) return "сегодня";
        if (days === 1) return "вчера";
        if (days < 7) return plural(days, ["день", "дня", "дней"]) + " назад";
        if (days < 30) return plural(Math.floor(days / 7), ["неделю", "недели", "недель"]) + " назад";
        return new Date(unix * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
    }

    function hashHue(text) {
        let x = 0;
        for (let i = 0; i < text.length; i++) x = (x * 31 + text.charCodeAt(i)) >>> 0;
        return x % 360;
    }

    // ------------------------------------------------------------------
    // События
    // ------------------------------------------------------------------

    const listeners = {};
    function on(event, fn) {
        (listeners[event] = listeners[event] || new Set()).add(fn);
        return () => listeners[event].delete(fn);
    }
    function emit(event, payload) {
        for (const fn of [...(listeners[event] || [])]) {
            try { fn(payload); } catch (e) { console.error(e); }
        }
    }

    // ------------------------------------------------------------------
    // API — только GET
    // ------------------------------------------------------------------

    function token() {
        try { return localStorage.getItem("token") || ""; } catch (e) { return ""; }
    }
    function headers() { return { Authorization: "Bearer " + token() }; }

    /* fresh=1 — мимо офлайн-кэшей service worker настоящего плеера (web/sw.js
     * пропускает такие запросы). Иначе обложки и тексты, пролистанные здесь,
     * вытесняли бы из его кэша то, что он хранит для работы без сети.
     * Сервер лишний параметр не замечает. */
    function fresh(path) { return path + (path.includes("?") ? "&" : "?") + "fresh=1"; }

    async function get(path) {
        const r = await fetch(fresh(path), { headers: headers() });
        if (r.status === 401 || r.status === 403) {
            const error = new Error("Нужен вход: откройте настоящий плеер и введите ключ");
            error.auth = true;
            throw error;
        }
        if (!r.ok) throw new Error("Сервер ответил " + r.status);
        return r.json();
    }

    // ------------------------------------------------------------------
    // Фонотека
    // ------------------------------------------------------------------

    const data = {
        ready: false,
        error: null,
        tracks: [],
        byPath: new Map(),
        artists: [],          // [{name, tracks, albums, cover}]
        artistByName: new Map(),
        albums: [],           // [{key, artist, album, tracks, cover, added}]
        albumByKey: new Map(),
        home: null,           // {moods, discover, albums}
        playlists: [],        // [{name, tracks, updated_at, cover}]
    };

    function mainArtist(t) {
        return (t.albumartist || t.artist || "").split(" • ")[0].trim() || "Без артиста";
    }
    function albumKey(artist, album) { return artist + "\u0000" + album; }

    function index(tracks) {
        data.tracks = tracks;
        data.byPath = new Map(tracks.map((t) => [t.path, t]));
        const artists = new Map();
        const albums = new Map();
        for (const t of tracks) {
            const name = mainArtist(t);
            let a = artists.get(name);
            if (!a) artists.set(name, (a = { name, tracks: [], albums: new Set(), cover: t.path, added: t.added || 0 }));
            a.tracks.push(t);
            a.added = Math.max(a.added, t.added || 0);
            if (t.album) {
                a.albums.add(t.album);
                const key = albumKey(name, t.album);
                let al = albums.get(key);
                if (!al) albums.set(key, (al = { key, artist: name, album: t.album, tracks: [], cover: t.path, added: 0 }));
                al.tracks.push(t);
                al.added = Math.max(al.added, t.added || 0);
            }
        }
        for (const al of albums.values()) {
            al.tracks.sort((x, y) => (x.track || 999) - (y.track || 999) || x.title.localeCompare(y.title, "ru"));
            al.cover = al.tracks[0].path;
        }
        data.artists = [...artists.values()].sort((x, y) => x.name.localeCompare(y.name, "ru", { sensitivity: "base" }));
        data.artistByName = artists;
        data.albums = [...albums.values()].sort((x, y) => y.added - x.added);
        data.albumByKey = albums;
    }

    async function load() {
        try {
            const [tracks, home, playlists] = await Promise.all([
                get("/api/library?limit=100000&sort=new"),
                get("/api/home").catch(() => null),
                get("/api/playlists").catch(() => []),
            ]);
            index(tracks);
            data.home = home;
            data.playlists = playlists;
            data.ready = true;
        } catch (e) {
            data.error = e;
        }
        emit("data");
    }

    /* Строка подборки — «Артист - Название» и путь. Из фонотеки берём
     * настоящие теги; трек, которого там уже нет, показываем как есть. */
    const playlistCache = new Map();
    async function playlistTracks(name) {
        if (playlistCache.has(name)) return playlistCache.get(name);
        const body = await get("/api/playlists/" + encodeURIComponent(name) + "/tracks");
        const tracks = (body.entries || []).map((e) => {
            const t = data.byPath.get(e.path);
            if (t) return t;
            const [artist, ...rest] = (e.title || "").split(" - ");
            return { path: e.path, artist: rest.length ? artist : "", title: rest.length ? rest.join(" - ") : e.title, duration: e.duration, missing: true };
        });
        const result = { name, tracks, hasCover: Boolean(body.cover) };
        playlistCache.set(name, result);
        return result;
    }

    function searchLocal(q, limit) {
        const needle = q.trim().toLowerCase();
        if (!needle) return { tracks: [], artists: [], albums: [] };
        const hit = (s) => (s || "").toLowerCase().includes(needle);
        return {
            tracks: data.tracks.filter((t) => hit(t.title) || hit(t.artist) || hit(t.album)).slice(0, limit || 50),
            artists: data.artists.filter((a) => hit(a.name)).slice(0, 12),
            albums: data.albums.filter((a) => hit(a.album) || hit(a.artist)).slice(0, 12),
        };
    }

    const lyricsCache = new Map();
    function lyrics(path) {
        if (!lyricsCache.has(path)) {
            lyricsCache.set(path, get("/api/lyrics?path=" + encodeURIComponent(path))
                .catch(() => ({ found: false, reason: "Текст не загрузился" })));
        }
        return lyricsCache.get(path);
    }

    // ------------------------------------------------------------------
    // Обложки
    // ------------------------------------------------------------------

    /* /api/cover требует токен, а <img src> заголовков не шлёт, поэтому
     * картинка берётся через fetch и подставляется blob-ссылкой. Не больше
     * шести запросов за раз и только для того, что видно на экране. */
    const coverUrls = new Map();     // key → Promise<string|null>
    const coverWaiting = [];
    let coverActive = 0;
    const SIZES = [96, 300, 600];

    function coverSize(px) {
        const want = px * Math.min(window.devicePixelRatio || 1, 2);
        return SIZES.find((s) => s >= want) || 600;
    }

    function coverUrl(path, px) {
        const size = coverSize(px || 300);
        const key = path + "|" + size;
        if (!coverUrls.has(key)) {
            coverUrls.set(key, new Promise((resolve) => {
                coverWaiting.push({ url: "/api/cover?path=" + encodeURIComponent(path) + "&size=" + size, resolve, forget: () => coverUrls.delete(key) });
                pumpCovers();
            }));
        }
        return coverUrls.get(key);
    }

    function playlistCoverUrl(name) {
        const key = "playlist|" + name;
        if (!coverUrls.has(key)) {
            coverUrls.set(key, new Promise((resolve) => {
                coverWaiting.push({ url: "/api/playlists/" + encodeURIComponent(name) + "/cover", resolve, forget: () => coverUrls.delete(key) });
                pumpCovers();
            }));
        }
        return coverUrls.get(key);
    }

    function pumpCovers() {
        while (coverActive < 6 && coverWaiting.length) {
            const job = coverWaiting.shift();
            coverActive++;
            fetch(fresh(job.url), { headers: headers() })
                .then((r) => {
                    // 404 — обложки правда нет; иное (сбой, 401) не запоминаем,
                    // чтобы следующий показ спросил снова.
                    if (!r.ok && r.status !== 404) job.forget();
                    return r.ok ? r.blob() : null;
                })
                .then((blob) => job.resolve(blob ? URL.createObjectURL(blob) : null))
                .catch(() => { job.forget(); job.resolve(null); })
                .finally(() => { coverActive--; pumpCovers(); });
        }
    }

    const coverObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
        for (const entry of entries) {
            if (!entry.isIntersecting) continue;
            coverObserver.unobserve(entry.target);
            fillCover(entry.target);
        }
    }, { rootMargin: "300px" }) : null;

    function fillCover(box) {
        const { path, px, playlist } = box.dataset;
        const promise = playlist ? playlistCoverUrl(playlist) : coverUrl(path, Number(px));
        promise.then((url) => {
            if (!url) { box.classList.add("is-missing"); return; }
            const img = new Image();
            img.alt = "";
            img.decoding = "async";
            img.onload = () => box.classList.add("is-loaded");
            img.src = url;
            box.appendChild(img);
        });
    }

    /* Квадрат обложки: пока картинки нет — цветная заглушка из имени
     * артиста (--ph-hue), чтобы сетка не мигала серым. */
    function cover(path, px, attrs) {
        const t = data.byPath.get(path);
        const seed = t ? mainArtist(t) : path;
        const box = h("div", Object.assign({ class: "cover" }, attrs || {}));
        box.style.setProperty("--ph-hue", String(hashHue(seed)));
        if (!path) { box.classList.add("is-missing"); return box; }
        box.dataset.path = path;
        box.dataset.px = String(px || 300);
        if (coverObserver) coverObserver.observe(box); else fillCover(box);
        return box;
    }

    /* Обложка подборки: своя картинка, если загружена, иначе мозаика из
     * первых четырёх треков. */
    function playlistCover(pl, px, attrs) {
        const box = h("div", Object.assign({ class: "cover cover-playlist" }, attrs || {}));
        box.style.setProperty("--ph-hue", String(hashHue(pl.name)));
        playlistCoverUrl(pl.name).then(async (url) => {
            if (url) {
                const img = new Image();
                img.alt = "";
                img.onload = () => box.classList.add("is-loaded");
                img.src = url;
                box.appendChild(img);
                return;
            }
            const list = await playlistTracks(pl.name).catch(() => null);
            const paths = list ? [...new Set(list.tracks.map((t) => t.path))].slice(0, 4) : [];
            if (paths.length < 4) {
                if (paths[0]) box.appendChild(cover(paths[0], px));
                else box.classList.add("is-missing");
                return;
            }
            box.classList.add("is-mosaic");
            for (const p of paths) box.appendChild(cover(p, Math.round((px || 300) / 2)));
        });
        return box;
    }

    /* Цвет обложки — средний по насыщенным точкам маленькой копии. */
    const colorCache = new Map();
    function coverColor(path) {
        if (!path) return Promise.resolve(null);
        if (!colorCache.has(path)) {
            colorCache.set(path, coverUrl(path, 48).then((url) => new Promise((resolve) => {
                if (!url) return resolve(null);
                const img = new Image();
                img.onload = () => {
                    try {
                        const c = document.createElement("canvas");
                        c.width = c.height = 24;
                        const g = c.getContext("2d", { willReadFrequently: true });
                        g.drawImage(img, 0, 0, 24, 24);
                        const px = g.getImageData(0, 0, 24, 24).data;
                        let r = 0, gg = 0, b = 0, w = 0, ar = 0, ag = 0, ab = 0;
                        for (let i = 0; i < px.length; i += 4) {
                            const R = px[i], G = px[i + 1], B = px[i + 2];
                            const max = Math.max(R, G, B), min = Math.min(R, G, B);
                            const sat = max ? (max - min) / max : 0;
                            const weight = sat * sat * (max / 255) + 0.0001;
                            r += R * weight; gg += G * weight; b += B * weight; w += weight;
                            ar += R; ag += G; ab += B;
                        }
                        const n = px.length / 4;
                        const result = w > 0.5
                            ? [Math.round(r / w), Math.round(gg / w), Math.round(b / w)]
                            : [Math.round(ar / n), Math.round(ag / n), Math.round(ab / n)];
                        resolve(result);
                    } catch (e) { resolve(null); }
                };
                img.onerror = () => resolve(null);
                img.src = url;
            })));
        }
        return colorCache.get(path);
    }

    // ------------------------------------------------------------------
    // Плеер
    // ------------------------------------------------------------------

    const audio = new Audio();
    audio.preload = "auto";
    const player = {
        audio,
        queue: [],
        index: -1,
        source: "",        // откуда очередь: «Подборка …», «Фонотека»
        shuffle: false,
        repeat: "off",     // off | all | one
        loading: false,
        error: "",
        order: [],         // порядок при перемешивании
        generation: 0,
        gainFactor: 1,     // поправка громкости трека (ReplayGain), только вниз
        userVolume: 1,     // громкость, выбранная человеком (где её можно менять)
    };
    const volumeAdjustable = (() => {
        const probe = new Audio();
        probe.volume = 0.5;
        return probe.volume === 0.5;
    })();

    const streamCache = new Map(); // path → {url, gain, expires}
    async function streamFor(path) {
        const cached = streamCache.get(path);
        /* Ссылки хватить должно на весь трек, а не на минуту: заранее взятая
         * для следующего трека могла пролежать всю длинную паузу. */
        const t = data.byPath.get(path);
        const need = ((t && t.duration) || 300) + 30;
        if (cached && cached.expires > Date.now() / 1000 + need) return cached;
        const body = await get("/api/stream-url?path=" + encodeURIComponent(path));
        const gain = typeof body.gain === "number" ? body.gain : null;
        const norm = !volumeAdjustable && gain !== null && gain < 0;
        const result = { url: norm ? body.url + "&norm=1" : body.url, gain, expires: body.expires_at || 0 };
        streamCache.set(path, result);
        return result;
    }

    function current() { return player.queue[player.index] || null; }

    async function playAt(i) {
        if (i < 0 || i >= player.queue.length) return;
        const generation = ++player.generation;
        player.index = i;
        player.loading = true;
        player.error = "";
        emit("track", current());
        emit("state");
        try {
            const stream = await streamFor(current().path);
            if (generation !== player.generation) return;
            player.gainFactor = stream.gain !== null ? Math.min(1, Math.pow(10, stream.gain / 20)) : 1;
            audio.volume = volumeAdjustable ? player.gainFactor * player.userVolume : 1;
            audio.src = stream.url;
            await audio.play();
        } catch (e) {
            if (generation !== player.generation || e.name === "AbortError") return;
            player.error = e.message || "Трек не включился";
            toast(player.error);
        } finally {
            if (generation === player.generation) player.loading = false;
            emit("state");
        }
        mediaSession();
        prefetchNext();
    }

    function prefetchNext() {
        const n = nextIndex();
        if (n >= 0 && player.queue[n]) streamFor(player.queue[n].path).catch(() => {});
    }

    function nextIndex() {
        if (!player.queue.length) return -1;
        if (player.repeat === "one") return player.index;
        const pos = player.order.indexOf(player.index);
        if (pos + 1 < player.order.length) return player.order[pos + 1];
        return player.repeat === "all" ? player.order[0] : -1;
    }

    function prevIndex() {
        const pos = player.order.indexOf(player.index);
        return pos > 0 ? player.order[pos - 1] : player.index;
    }

    function shuffledOrder(n, first) {
        const rest = [...Array(n).keys()].filter((i) => i !== first);
        for (let i = rest.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [rest[i], rest[j]] = [rest[j], rest[i]];
        }
        return first >= 0 ? [first, ...rest] : rest;
    }

    /* play(список, с какого, {shuffle, source}) — новая очередь. */
    function playable(t) { return Boolean(t && t.path && !t.missing); }

    function play(tracks, start, opts) {
        const all = tracks || [];
        // Индекс — в списке, который видит человек; удалённые из фонотеки
        // строки выпадают, поэтому ищем сам нажатый трек, а не его номер.
        const target = typeof start === "number" && start >= 0 ? all[start] : null;
        const list = all.filter(playable);
        if (!list.length) return;
        if (target && !playable(target)) { toast("Этого трека уже нет в фонотеке"); return; }
        opts = opts || {};
        player.queue = list;
        player.source = opts.source || "";
        if (opts.shuffle !== undefined) player.shuffle = Boolean(opts.shuffle);
        let first = target ? list.indexOf(target) : -1;
        if (first < 0 && player.shuffle) first = Math.floor(Math.random() * list.length);
        if (first < 0) first = 0;
        player.order = player.shuffle ? shuffledOrder(list.length, first) : [...list.keys()];
        emit("queue");
        playAt(first);
    }

    function toggle() {
        if (!current()) return;
        if (!audio.src) { playAt(player.index); return; }
        if (audio.paused) audio.play().catch((e) => toast(e.message)); else audio.pause();
    }

    /* Кнопка «дальше» уходит на следующий трек и при повторе одного:
     * повтор держит трек только до его естественного конца. */
    function next() {
        const pos = player.order.indexOf(player.index);
        const n = pos + 1 < player.order.length ? player.order[pos + 1]
            : player.repeat !== "off" ? player.order[0] : -1;
        if (n >= 0) playAt(n);
    }
    function prev() {
        if (audio.currentTime > 3) { audio.currentTime = 0; return; }
        playAt(prevIndex());
    }
    function seek(sec) {
        if (!Number.isFinite(sec)) return;
        audio.currentTime = Math.max(0, Math.min(sec, duration() || sec));
        emit("time");
    }
    function duration() {
        const d = audio.duration;
        if (Number.isFinite(d) && d > 0) return d;
        return (current() && current().duration) || 0;
    }
    function setShuffle(onOff) {
        player.shuffle = onOff === undefined ? !player.shuffle : Boolean(onOff);
        player.order = player.shuffle ? shuffledOrder(player.queue.length, player.index) : [...player.queue.keys()];
        emit("queue");
        emit("state");
    }
    function cycleRepeat() {
        player.repeat = player.repeat === "off" ? "all" : player.repeat === "all" ? "one" : "off";
        emit("state");
    }
    /* «Играть следующим» и «в очередь» меняют только очередь этой вкладки —
     * это не запись на сервер, поэтому в макете работают по-настоящему. */
    function playNext(track) {
        if (!playable(track)) { toast("Этого трека уже нет в фонотеке"); return; }
        if (!current()) { play([track], 0); return; }
        player.queue.push(track);
        const idx = player.queue.length - 1;
        const pos = player.order.indexOf(player.index);
        player.order.splice(pos + 1, 0, idx);
        emit("queue");
        toast("Следующим: " + track.title);
    }
    function enqueue(track) {
        if (!playable(track)) { toast("Этого трека уже нет в фонотеке"); return; }
        if (!current()) { play([track], 0); return; }
        player.queue.push(track);
        player.order.push(player.queue.length - 1);
        emit("queue");
        toast("В очереди: " + track.title);
    }
    function upcoming() {
        const pos = player.order.indexOf(player.index);
        return player.order.slice(pos + 1).map((i) => ({ track: player.queue[i], index: i }));
    }

    audio.addEventListener("play", () => emit("state"));
    audio.addEventListener("pause", () => emit("state"));
    audio.addEventListener("waiting", () => { player.loading = true; emit("state"); });
    audio.addEventListener("playing", () => { player.loading = false; errorSkips = 0; emit("state"); });
    audio.addEventListener("timeupdate", () => emit("time"));
    audio.addEventListener("loadedmetadata", () => emit("time"));
    /* Ссылка истекла или поток оборвался: один раз берём новую ссылку и
     * продолжаем с того же места, второй сбой подряд — следующий трек. */
    let retriedGeneration = -1;
    let errorSkips = 0;
    audio.addEventListener("error", async () => {
        const t = current();
        if (!t || !audio.getAttribute("src")) return;
        const generation = player.generation;
        if (retriedGeneration === generation) {
            // Не больше трёх пропусков подряд: без сети иначе пробежали бы всю очередь.
            if (++errorSkips > 3) { toast("Треки не играют — проверьте связь с компьютером"); player.loading = false; emit("state"); return; }
            toast("«" + t.title + "» не играет — включаю следующий");
            const n = nextIndex();
            if (n >= 0 && n !== player.index) playAt(n); else { player.loading = false; emit("state"); }
            return;
        }
        retriedGeneration = generation;
        const at = audio.currentTime || 0;
        streamCache.delete(t.path);
        try {
            const stream = await streamFor(t.path);
            if (generation !== player.generation) return;
            audio.src = stream.url;
            audio.currentTime = at;
            await audio.play();
        } catch (e) { /* второй error сработает сам */ }
    });
    audio.addEventListener("ended", () => {
        const n = nextIndex();
        if (n >= 0) playAt(n); else emit("state");
    });

    function mediaSession() {
        if (!("mediaSession" in navigator) || !window.MediaMetadata) return;
        const t = current();
        if (!t) return;
        const meta = { title: t.title, artist: t.artist || "", album: t.album || "" };
        navigator.mediaSession.metadata = new MediaMetadata(meta);
        coverUrl(t.path, 300).then((url) => {
            if (url && current() === t) {
                navigator.mediaSession.metadata = new MediaMetadata(Object.assign(meta, { artwork: [{ src: url, sizes: "300x300" }] }));
            }
        });
    }
    if ("mediaSession" in navigator) {
        const set = (a, fn) => { try { navigator.mediaSession.setActionHandler(a, fn); } catch (e) { /* нет */ } };
        set("play", () => toggle());
        set("pause", () => toggle());
        set("nexttrack", () => next());
        set("previoustrack", () => prev());
        set("seekto", (d) => seek(d.seekTime));
    }

    // ------------------------------------------------------------------
    // Адреса экранов
    // ------------------------------------------------------------------

    /* #/library?tab=artists, #/artist/<имя>, #/album/<артист>/<альбом>,
     * #/playlists, #/playlist/<имя>, #/search?q=, #/add, #/service, #/ (главная).
     * Адрес общий для всех вариантов: переключили — остались на том же экране. */
    function parseRoute() {
        const raw = location.hash.replace(/^#/, "") || "/";
        const [pathPart, queryPart] = raw.split("?");
        const parts = pathPart.split("/").filter(Boolean).map((p) => {
            try { return decodeURIComponent(p); } catch (e) { return p; }
        });
        const query = Object.fromEntries(new URLSearchParams(queryPart || ""));
        const name = parts[0] || "home";
        return { name, params: parts.slice(1), query, raw };
    }
    const route = { current: parseRoute() };

    function href(name, ...params) {
        let query = "";
        if (params.length && typeof params[params.length - 1] === "object") {
            query = "?" + new URLSearchParams(params.pop()).toString();
        }
        if (name === "home") return "#/" + query;
        return "#/" + [name, ...params.map((p) => encodeURIComponent(p))].join("/") + query;
    }
    let backPending = false;
    let pendingTarget = null;
    function go(name, ...params) {
        const target = href(name, ...params);
        /* closeNow() только что попросил history.back(): переход сейчас
         * записал бы новый адрес, а запоздавший back() вернул бы с него. */
        if (backPending) { pendingTarget = target; return; }
        if (location.hash === target) { emit("route", route.current); return; }
        location.hash = target;
    }
    /* Поправить адрес без нового шага в истории (поиск по мере набора). */
    function replaceRoute(name, ...params) {
        history.replaceState(history.state, "", href(name, ...params));
        route.current = parseRoute();
    }
    window.addEventListener("hashchange", () => {
        route.current = parseRoute();
        emit("route", route.current);
    });

    /* Экран «сейчас играет» — поверх любого другого. Своя запись в истории,
     * чтобы «назад» на телефоне закрывал его, а не уводил со страницы. */
    const ui = { now: false, nowTab: "cover" };
    function openNow(tab) {
        if (!current()) return;
        if (tab) ui.nowTab = tab;
        if (!ui.now) {
            ui.now = true;
            history.pushState({ labNow: true }, "");
        }
        emit("now", ui);
    }
    function closeNow() {
        if (!ui.now) return;
        ui.now = false;
        if (history.state && history.state.labNow) { backPending = true; history.back(); }
        emit("now", ui);
    }
    function setNowTab(tab) { ui.nowTab = tab; emit("now", ui); }
    window.addEventListener("popstate", () => {
        if (backPending) {
            backPending = false;
            if (pendingTarget) {
                const target = pendingTarget;
                pendingTarget = null;
                if (location.hash !== target) location.hash = target;
            }
        }
        if (ui.now && !(history.state && history.state.labNow)) {
            ui.now = false;
            emit("now", ui);
        }
    });

    // ------------------------------------------------------------------
    // Подсказки и заглушки действий
    // ------------------------------------------------------------------

    let toastEl = null;
    let toastTimer = 0;
    function toast(message) {
        if (!toastEl) {
            toastEl = h("div", { class: "lab-toast", role: "status", "aria-live": "polite" });
            document.body.appendChild(toastEl);
        }
        toastEl.textContent = message;
        toastEl.classList.add("is-on");
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => toastEl.classList.remove("is-on"), 2600);
    }
    /* Кнопка, которая в настоящем плеере что-то меняет. */
    function act(label) {
        toast("«" + label + "» — в макете только показано, ничего не изменилось");
    }

    /* Действия с треком — одинаковые во всех вариантах, различается только
     * то, как вариант их рисует (лист снизу, выпадающее меню, строка). */
    function trackActions(track) {
        const artist = data.artistByName.get(mainArtist(track));
        const album = track.album ? data.albumByKey.get(albumKey(mainArtist(track), track.album)) : null;
        return [
            { id: "next", label: "Играть следующим", icon: "queue", run: () => playNext(track) },
            { id: "queue", label: "Добавить в очередь", icon: "list", run: () => enqueue(track) },
            artist && { id: "artist", label: "Перейти к артисту", icon: "artist", run: () => { closeNow(); go("artist", artist.name); } },
            album && album.tracks.length > 1 && { id: "album", label: "Перейти к альбому", icon: "album", run: () => { closeNow(); go("album", album.artist, album.album); } },
            { id: "playlist", label: "Добавить в подборку", icon: "playlists", run: () => act("Добавить в подборку") },
            { id: "lyrics", label: "Текст песни", icon: "lyrics", run: () => { if (current() !== track) play([track], 0); openNow("lyrics"); } },
            { id: "replace", label: "Заменить версию", icon: "link", run: () => act("Заменить версию") },
            { id: "delete", label: "Удалить из фонотеки", icon: "trash", danger: true, run: () => act("Удалить из фонотеки") },
        ].filter(Boolean);
    }

    // ------------------------------------------------------------------
    // Варианты и переключатель
    // ------------------------------------------------------------------

    const variants = [];
    let active = null;      // {variant, cleanups}
    const stage = () => document.getElementById("stage");

    function register(variant) { variants.push(variant); }

    function mountVariant(i) {
        if (active) {
            for (const off of active.cleanups) { try { off(); } catch (e) { /* уже */ } }
            if (active.variant.unmount) active.variant.unmount();
        }
        const variant = variants[i];
        for (const link of document.querySelectorAll("link[data-variant]")) {
            link.disabled = link.dataset.variant !== variant.id;
        }
        document.documentElement.dataset.variant = variant.id;
        const meta = document.querySelector('meta[name="theme-color"]');
        if (meta && variant.themeColor) meta.content = variant.themeColor;
        const root = clear(stage());
        root.className = "stage " + variant.id;
        const cleanups = [];
        const ctx = {
            root,
            on(event, fn) { const off = on(event, fn); cleanups.push(off); return off; },
            cleanup(fn) { cleanups.push(fn); },
        };
        active = { variant, cleanups };
        variant.mount(ctx);
    }

    function startPicker() {
        const picker = document.querySelector(".proto-picker");
        const highlight = picker.querySelector(".proto-picker-highlight");
        clear(picker, highlight);
        const items = variants.map((v, i) => h("button", {
            class: "proto-picker-item", type: "button", title: v.name + " — клавиша " + (i + 1),
            onclick: () => setActive(i),
        }, v.name));
        append(picker, items);
        let currentIndex = 0;

        function moveHighlight() {
            const el = items[currentIndex];
            highlight.style.width = el.offsetWidth + "px";
            highlight.style.transform = `translateX(${el.offsetLeft}px)`;
        }
        function setActive(i) {
            if (i < 0 || i >= variants.length) return;
            currentIndex = i;
            items.forEach((el, j) => {
                el.toggleAttribute("data-active", j === i);
                if (j === i) el.setAttribute("aria-current", "true"); else el.removeAttribute("aria-current");
            });
            moveHighlight();
            const url = new URL(location);
            url.searchParams.set("v", i + 1);
            history.replaceState(history.state, "", url);
            mountVariant(i);
        }
        window.addEventListener("resize", moveHighlight);
        document.addEventListener("keydown", (e) => {
            if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable) return;
            if (e.metaKey || e.ctrlKey || e.altKey) return;
            const num = parseInt(e.key, 10);
            if (num >= 1 && num <= variants.length) setActive(num - 1);
            else if (e.key === "ArrowRight" && e.shiftKey) setActive((currentIndex + 1) % variants.length);
            else if (e.key === "ArrowLeft" && e.shiftKey) setActive((currentIndex - 1 + variants.length) % variants.length);
            else if (e.key === " " && !(e.target instanceof HTMLButtonElement)) { e.preventDefault(); toggle(); }
        });
        setActive((parseInt(new URLSearchParams(location.search).get("v"), 10) || 1) - 1);
        requestAnimationFrame(() => requestAnimationFrame(() => picker.setAttribute("data-ready", "")));
    }

    function start() {
        if (!token()) {
            clear(stage(), h("div", { class: "lab-gate" },
                h("p", null, "Ключа нет в этом браузере."),
                h("p", null, "Откройте настоящий плеер по этому же адресу, войдите, и вернитесь сюда."),
                h("a", { href: "/" }, "Открыть плеер")));
            return;
        }
        startPicker();
        load();
    }

    window.Lab = {
        h, clear, append, icon, fmtTime, fmtTotal, plural, agoText, hashHue,
        on, emit, data, load, playlistTracks, searchLocal, lyrics,
        tasks: () => get("/api/tasks"),
        health: () => get("/health"),
        searchYouTube: (q) => get("/api/search?q=" + encodeURIComponent(q) + "&limit=8"),
        cover, playlistCover, coverUrl, coverColor, mainArtist, albumKey,
        player, play, playAt, toggle, next, prev, seek, duration, current, setShuffle, cycleRepeat,
        playNext, enqueue, upcoming, volumeAdjustable,
        route, href, go, replaceRoute, ui, openNow, closeNow, setNowTab,
        toast, act, register, start, trackActions,
    };
})();
