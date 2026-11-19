/* «Починка» — карточки над треками «Клипов» (adder/repair.py, этап 3 плана
 * 19.11.2026). Трек положили в «Клипы» — служба в фоне ищет другие версии
 * той же песни, сравнивает их с файлом и пишет, что не так и какая версия
 * лучше. Здесь это видно и решается: «послушать место» у обеих версий,
 * «Заменить», «Вставить ссылку», «Оставить как есть».
 * Тексты — только через textContent (данные приходят из YouTube). */

const REPAIR_PLAYLIST = "Клипы";
const REPAIR_POLL_MS = 15000;
const REPAIR_KINDS = { studio: "студийная", clip: "официальный клип", upload: "чужая загрузка" };

let repairItems = [];
let repairTimer = null;
let repairLoading = null;
let repairSound = null;

function repairOnScreen() {
    return activeView === "viewPlaylist" && player.playlist && player.playlist.name === REPAIR_PLAYLIST;
}

async function loadRepairs() {
    if (repairLoading) return repairLoading;
    repairLoading = (async () => {
        try {
            const r = await fetch("/api/repairs", { headers: headers(), cache: "no-store" });
            if (!r.ok) return;
            const data = await r.json();
            repairItems = data.items || [];
        } catch (e) {
            /* без сети — покажем, что было */
        }
    })().finally(() => { repairLoading = null; });
    return repairLoading;
}

/* Зовётся из renderPlaylist: у «Клипов» — карточки, у остальных — ничего. */
async function renderRepairs() {
    const box = document.getElementById("playlistRepair");
    if (!box) return;
    clearTimeout(repairTimer);
    if (!repairOnScreen()) {
        box.hidden = true;
        box.replaceChildren();
        return;
    }
    await loadRepairs();
    if (!repairOnScreen()) return;
    drawRepairs(box);
    if (repairItems.some(i => i.status === "queued" || i.status === "working")) {
        repairTimer = setTimeout(renderRepairs, REPAIR_POLL_MS);
    }
}

function drawRepairs(box) {
    box.hidden = !repairItems.length;
    if (!repairItems.length) { box.replaceChildren(); return; }
    const head = document.createElement("div");
    head.className = "repair-head";
    const title = document.createElement("h3");
    title.textContent = "Починка";
    const hint = document.createElement("p");
    hint.textContent = "Служба ищет другие версии каждого трека отсюда и сравнивает их со звуком в фонотеке. "
        + "После замены трек сам уходит из «Клипов».";
    head.append(title, hint);
    box.replaceChildren(head, ...repairItems.map(repairCard));
}

function repairEntry(path) {
    const entries = (player.playlist && player.playlist.entries) || [];
    return entries.find(e => e.path === path) || { path, title: path };
}

function repairLine(cls, text) {
    const p = document.createElement("p");
    p.className = cls;
    p.textContent = text;
    return p;
}

function repairButton(label, cls, onclick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.textContent = label;
    b.onclick = onclick;
    return b;
}

function repairCard(item) {
    const card = document.createElement("div");
    card.className = "confirm repair-card";
    card.dataset.repairPath = item.path;
    const names = trackNames(repairEntry(item.path));
    const name = document.createElement("div");
    name.className = "track-title";
    name.textContent = names.title;
    const who = document.createElement("div");
    who.className = "track-artist";
    who.textContent = names.artist || "";
    card.append(name, who);

    const note = repairLine("repair-note", "");
    const row = document.createElement("div");
    row.className = "row wrap";

    if (item.auto && item.reason) card.appendChild(repairLine("repair-why", "Положила служба: " + item.reason));

    if (item.status === "queued" || item.status === "working") {
        card.appendChild(repairLine("repair-status",
            item.status === "working" ? "Ищу и сравниваю версии…"
                : (item.error ? "Жду: " + item.error : "В очереди — версии ищутся в фоне, по одной, без спешки.")));
    } else if (item.status === "failed") {
        card.appendChild(repairLine("repair-status", "Не вышло: " + (item.error || "неизвестная ошибка")));
        row.appendChild(repairButton("Повторить", "ghost", () => repairAction("retry", item, card, note)));
    } else if (item.status === "ready") {
        const problems = item.problems || [];
        if (problems.length) {
            const list = document.createElement("ul");
            list.className = "repair-problems";
            for (const p of problems) {
                const li = document.createElement("li");
                li.textContent = p.text;
                list.appendChild(li);
            }
            card.appendChild(list);
        } else if (item.compared) {
            card.appendChild(repairLine("repair-status", "Явных проблем не нашлось."));
        }
        if (item.lyrics_note) card.appendChild(repairLine("repair-why", "Текст: " + item.lyrics_note));
        const best = (item.candidates || []).find(c => c.video_id === item.best);
        if (best) {
            const kind = REPAIR_KINDS[best.kind] || best.kind;
            const length = best.duration ? " · " + formatTime(best.duration) : "";
            card.appendChild(repairLine("repair-best",
                (item.better ? "Лучшая версия: " : "Другая версия: ") + kind + " «" + (best.title || "") + "»" + length));
            if (!item.better) card.appendChild(repairLine("repair-why", "Не очевидно, что она лучше, — решай на слух."));
            const listen = item.listen || {};
            const at = item.place ? " (" + formatTime(item.place.at) + ")" : "";
            if (listen.current) row.appendChild(repairButton("▶ Сейчас" + at, "ghost", () => repairListen(item.path, "current", note)));
            /* Не совпала по звуку — её «то же место» посчитано по случайному
             * сдвигу: кусок кандидата был бы не про то. */
            const matched = best.same !== false;
            if (listen.candidate && matched) row.appendChild(repairButton("▶ Кандидат", "ghost", () => repairListen(item.path, "candidate", note)));
            row.appendChild(repairButton("Заменить", item.better ? "ghost repair-main" : "ghost", () => repairAction("replace", item, card, note)));
        } else {
            card.appendChild(repairLine("repair-status", "Других версий не нашлось — вставь ссылку сам."));
        }
    }
    row.appendChild(repairButton("Вставить ссылку", "ghost", () => repairPasteLink(item.path, note)));
    row.appendChild(repairButton("Оставить как есть", "ghost", () => repairAction("keep", item, card, note)));
    card.append(row, note);
    return card;
}

async function repairListen(path, which, note) {
    note.textContent = "";
    try {
        const r = await fetch("/api/repairs/listen?path=" + encodeURIComponent(path) + "&which=" + which,
            { headers: headers(), cache: "no-store" });
        if (!r.ok) throw new Error("Кусок не загрузился");
        const url = URL.createObjectURL(await r.blob());
        if (repairSound) { repairSound.pause(); URL.revokeObjectURL(repairSound.src); }
        /* Плеер — на паузу: два звука сразу не сравнить. */
        if (!player.audio.paused) player.audio.pause();
        repairSound = new Audio(url);
        await repairSound.play();
    } catch (e) {
        note.textContent = e.message || "Не играет";
    }
}

async function repairAction(what, item, card, note) {
    for (const b of card.querySelectorAll("button")) b.disabled = true;
    note.textContent = what === "replace" ? "Заменяю…" : "";
    try {
        const r = await fetch("/api/repairs/" + what, {
            method: "POST",
            headers: { ...headers(), "Content-Type": "application/json" },
            body: JSON.stringify({ path: item.path, video_id: item.best || "" }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || "Не вышло");
        /* Играет или стоит в очереди — звук и длина новые (как после
         * «Заменить по ссылке»). */
        if (what === "replace" && typeof applyReplacement === "function") await applyReplacement(item.path, item.path);
        /* Записка — после перезагрузки: openPlaylist её стирает. */
        await openPlaylist(REPAIR_PLAYLIST);
        if (what === "replace") setPlaylistNote((data.note || "Заменено") + ". Трек убран из «Клипов».");
        else if (what === "keep") setPlaylistNote("Оставлен как есть и убран из «Клипов».");
    } catch (e) {
        for (const b of card.querySelectorAll("button")) b.disabled = false;
        note.textContent = e.message;
    }
}

/* «Вставить ссылку» — тот же «Заменить по ссылке», что в «⋯» строки. */
function repairPasteLink(path, note) {
    const rows = document.querySelectorAll("#playlistTracks .playlist-track");
    const row = Array.from(rows).find(r => r.dataset.trackPath === path);
    const more = row && row.querySelector("button[aria-haspopup='menu']");
    if (!row || !more) { note.textContent = "Строки трека не видно — сбрось поиск по подборке."; return; }
    row.scrollIntoView({ block: "center" });
    askForReplacement(more, Number(row.dataset.index));
}
