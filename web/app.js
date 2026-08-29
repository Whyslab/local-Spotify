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
}

/* ---------------- Views ---------------- */

function switchView(id) {
    activeView = id;
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
    if (activeView === "viewAdd") tasks();
    if (activeView === "viewLibrary") library();
}

/* ---------------- Adding ---------------- */

function clearInput() {
    document.getElementById("links").value = "";
    document.getElementById("addResult").textContent = "";
}

async function addTracks() {
    const field = document.getElementById("links");
    const links = field.value.split("\n").map(x => x.trim()).filter(Boolean);
    const result = document.getElementById("addResult");

    if (!links.length) {
        result.textContent = "Вставь хотя бы одну ссылку.";
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

async function tasks() {
    const box = document.getElementById("tasks");
    const empty = document.getElementById("tasksEmpty");
    const count = document.getElementById("queueCount");

    try {
        const r = await fetch("/api/tasks", { headers: headers() });
        if (!r.ok) return;
        const data = await r.json();

        count.textContent = data.length ? `${data.length}` : "";
        empty.hidden = data.length > 0;
        box.replaceChildren();

        for (const t of data) {
            box.appendChild(trackRow({
                title: t.title || t.url || "—",
                artist: t.artist || "",
                status: t.status,
                error: t.error,
            }));
        }
    } catch (e) {
        // Polling loop - a transient network hiccup shouldn't throw to console.
    }
}

function trackRow({ title, artist, status, error }) {
    const card = document.createElement("div");
    card.className = "track";

    const cover = document.createElement("div");
    cover.className = "cover";

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

    card.append(cover, info, badge);
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

async function library() {
    const box = document.getElementById("library");
    const empty = document.getElementById("libraryEmpty");
    const count = document.getElementById("libraryCount");
    const q = document.getElementById("librarySearch").value.trim();

    try {
        const r = await fetch("/api/library?q=" + encodeURIComponent(q), { headers: headers() });
        if (!r.ok) return;
        const data = await r.json();

        count.textContent = data.length ? data.length + (data.length === 200 ? "+" : "") : "";
        empty.hidden = data.length > 0;
        box.replaceChildren();

        for (const t of data) {
            box.appendChild(libraryRow(t));
        }
    } catch (e) {
        // Same reasoning as tasks(): the next keystroke or poll retries.
    }
}

function libraryRow(t) {
    const card = document.createElement("div");
    card.className = "track";

    const cover = document.createElement("div");
    cover.className = "cover";
    if (t.track) cover.textContent = String(t.track);

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

    if (t.album) {
        const albumEl = document.createElement("div");
        albumEl.className = "track-album";
        albumEl.textContent = t.album;
        info.appendChild(albumEl);
    }

    const del = document.createElement("button");
    del.className = "icon-button";
    del.setAttribute("aria-label", "Удалить");
    del.onclick = () => askRemove(card, t);

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("viewBox", "0 0 24 24");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6");
    svg.appendChild(path);
    del.appendChild(svg);

    card.append(cover, info, del);
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

applyLoginState();
refresh();
setInterval(refresh, POLL_MS);
