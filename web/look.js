/* Оформление «Обложка» (с 01.10.2026): то, чего нет в CSS.
 *
 * 1. Цвет из обложки. Акцент и фон большого плеера берутся из обложки того,
 *    что играет. Выключается в «Служба → Оформление» и в «⋯» плеера; без него
 *    тема прежняя, чёрно-белая. Выбор живёт в этом браузере.
 * 2. Большой плеер на телефоне. Панель #player внизу — мини-плеер; нажатие
 *    раскрывает её во весь экран, жест вниз или «назад» сворачивают. Текст
 *    песни и очередь на это время переезжают внутрь неё: те же узлы, что и в
 *    середине страницы, поэтому player.js продолжает вести их по своим id.
 *
 * Всё, что приходит из тегов, сюда не попадает: здесь только цвета, классы
 * и перестановка уже готовых узлов.
 */

/* ---------------- Цвет из обложки ---------------- */

const TINT_KEY = "coverTint";
let tintRgb = null;
let tintFor = null;

function tintEnabled() {
    try { return localStorage.getItem(TINT_KEY) !== "0"; } catch (e) { return true; }
}

function toHsl([r, g, b]) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    const l = (max + min) / 2;
    let h = 0, s = 0;
    if (max !== min) {
        const d = max - min;
        s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
        h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
        h *= 60;
    }
    return [Math.round(h), Math.round(s * 100), Math.round(l * 100)];
}

/* Акцент — это и текст на серых плашках (#1c1c1e, материал вкладок), не
 * только на чёрном: светлота 74% держит от 4,5 : 1 и там при любом оттенке
 * (68% хватало только на чёрном). Фон большого плеера — тёмный под белым. */
function applyTint() {
    const app = document.querySelector(".app");
    if (!app) return;
    /* Почти серая обложка (насыщенность ниже 15%) цвета не даёт: серый акцент
     * был тусклее белого текста, и играющая строка терялась. Тогда — как без
     * цвета, белым. */
    const on = tintEnabled() && tintRgb && toHsl(tintRgb)[1] >= 15;
    for (const box of document.querySelectorAll("#coverTintSwitch")) box.checked = tintEnabled();
    if (!on) {
        app.classList.remove("is-tinted");
        for (const name of ["--tint", "--tint-deep", "--tint-mid"]) app.style.removeProperty(name);
        document.documentElement.style.removeProperty("--glow");
        return;
    }
    const [h, s] = toHsl(tintRgb);
    const sat = Math.max(55, Math.min(90, s + 20));
    app.style.setProperty("--tint", `hsl(${h} ${sat}% 74%)`);
    app.style.setProperty("--tint-deep", `hsl(${h} ${Math.min(sat, 55)}% 14%)`);
    app.style.setProperty("--tint-mid", `hsl(${(h + 18) % 360} ${Math.min(sat, 60)}% 28%)`);
    /* Подсветка за шапкой — у body, вне .app: ей цвет отдаётся отдельно. */
    document.documentElement.style.setProperty("--glow", `hsl(${h} ${sat}% 60%)`);
    app.classList.add("is-tinted");
}

function setCoverTint(on) {
    try { localStorage.setItem(TINT_KEY, on ? "1" : "0"); } catch (e) { /* приватное окно */ }
    applyTint();
}

/* Средний цвет по насыщенным точкам уменьшенной копии: фон обложки обычно
 * тёмный или белый, и простое среднее давало грязно-серый почти всегда. */
function colorOf(url) {
    return new Promise((resolve) => {
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
                resolve(w > 0.5 ? [r / w, gg / w, b / w].map(Math.round)
                    : [ar / n, ag / n, ab / n].map(Math.round));
            } catch (e) { resolve(null); }
        };
        img.onerror = () => resolve(null);
        img.src = url;
    });
}

/* player.js сообщает, что обложка играющего трека загрузилась (или что её нет). */
document.addEventListener("nowcover", (event) => {
    const url = event.detail && event.detail.url;
    const art = document.getElementById("playerArt");
    if (art) {
        art.style.backgroundImage = url ? `url("${url}")` : "";
        art.classList.toggle("is-empty", !url);
    }
    const bg = document.getElementById("player");
    if (bg) bg.style.setProperty("--art", url ? `url("${url}")` : "none");
    tintFor = url;
    if (!url) { tintRgb = null; applyTint(); return; }
    colorOf(url).then((rgb) => {
        if (tintFor !== url) return;   // трек уже сменился
        tintRgb = rgb;
        applyTint();
    });
});

/* ---------------- Большой плеер на телефоне ---------------- */

const WIDE = window.matchMedia("(min-width: 1100px)");
let sheetPanel = null;          // "lyrics" | "queue" | null
const homes = new Map();        // узел → {parent, next}: куда вернуть

function sheetOpen() {
    const app = document.querySelector(".app");
    return Boolean(app && app.classList.contains("player-open"));
}

function expandPlayer(from) {
    if (WIDE.matches) {
        /* На компьютере большой обложке есть место в правой колонке, а
         * нажатие по мини-обложке ведёт к тексту — туда же, куда кнопка.
         * Нажатие по названию там ничего не делает: его выделяют мышью. */
        if (from === "art") toggleLyricsView();
        return;
    }
    const app = document.querySelector(".app");
    if (!app || sheetOpen() || document.getElementById("player").hidden) return;
    app.classList.add("player-open");
    document.documentElement.classList.add("sheet-lock");
    setModal(true);
    /* Своя запись в истории: «назад» на телефоне сворачивает плеер, а не
     * уводит со страницы. */
    history.pushState({ playerOpen: true }, "");
    document.getElementById("playerGrab").focus({ preventScroll: true, focusVisible: false });
}

/* Раскрытый плеер — окно поверх страницы: диктор называет его диалогом, а
 * Tab не уходит в разделы под ним (inert на всём, кроме плеера). */
function setModal(on) {
    const app = document.querySelector(".app");
    const bar = document.getElementById("player");
    const art = document.getElementById("playerArt");
    for (const child of app.children) {
        if (child !== bar) child.inert = on;
    }
    if (on) {
        bar.setAttribute("role", "dialog");
        bar.setAttribute("aria-modal", "true");
        bar.setAttribute("aria-label", "Сейчас играет");
        /* Обложка в раскрытом плеере — картинка, а не кнопка. */
        art.tabIndex = -1;
    } else {
        bar.removeAttribute("role");
        bar.removeAttribute("aria-modal");
        bar.removeAttribute("aria-label");
        art.removeAttribute("tabindex");
    }
}

function collapsePlayer(fromHistory) {
    const app = document.querySelector(".app");
    if (!app || !sheetOpen()) return;
    const bar = document.getElementById("player");
    const hadFocus = bar.contains(document.activeElement) || document.activeElement === document.body;
    showInSheet(null);
    app.classList.remove("player-open");
    document.documentElement.classList.remove("sheet-lock");
    setModal(false);
    /* Фокус — туда, откуда плеер открывали, а не в начало страницы. */
    if (hadFocus && !bar.hidden) document.getElementById("playerArt").focus({ preventScroll: true, focusVisible: false });
    if (!fromHistory && history.state && history.state.playerOpen) history.back();
}

window.addEventListener("popstate", () => {
    if (sheetOpen() && !(history.state && history.state.playerOpen)) collapsePlayer(true);
});

/* Узел переезжает в большой плеер и возвращается на своё место ровно туда,
 * откуда взят: соседний узел запоминается, чтобы порядок в странице не плыл. */
function borrow(node, into) {
    if (!homes.has(node)) homes.set(node, { parent: node.parentNode, next: node.nextSibling });
    into.appendChild(node);
    /* Очередь лежит прямо в .app, и раскрытие плеера отключило её (inert)
     * вместе со всей страницей. Внутри плеера она снова нужна. */
    node.inert = false;
}

function giveBack(node) {
    const home = homes.get(node);
    if (!home) return;
    home.parent.insertBefore(node, home.next && home.next.parentNode === home.parent ? home.next : null);
    homes.delete(node);
    /* Вернулся прямым ребёнком .app, а плеер ещё раскрыт — снова под inert. */
    if (sheetOpen() && home.parent === document.querySelector(".app")) node.inert = true;
}

function showInSheet(kind) {
    const panel = document.getElementById("playerPanel");
    const lyricsView = document.getElementById("viewLyrics");
    const queue = document.getElementById("playQueue");
    const bar = document.getElementById("player");
    const lyricsButton = document.getElementById("playerLyricsButton");
    const queueButton = document.getElementById("playerQueueButton");

    if (sheetPanel === "lyrics" && kind !== "lyrics") {
        giveBack(lyricsView);
        lyricsView.hidden = activeView !== "viewLyrics";
    }
    if (sheetPanel === "queue" && kind !== "queue") {
        giveBack(queue);
        if (!queue.hidden) toggleQueuePanel();
    }

    if (kind === "lyrics" && sheetPanel !== "lyrics") {
        borrow(lyricsView, panel);
        lyricsView.hidden = false;
        const track = player.queue[player.index];
        if (track) loadLyrics(track);
    }
    if (kind === "queue" && sheetPanel !== "queue") {
        borrow(queue, panel);
        if (queue.hidden) toggleQueuePanel();
    }

    sheetPanel = kind;
    bar.classList.toggle("is-lyrics", kind === "lyrics");
    bar.classList.toggle("is-queue", kind === "queue");
    if (lyricsButton) {
        const on = kind === "lyrics" || (!sheetOpen() && activeView === "viewLyrics");
        lyricsButton.classList.toggle("is-on", on);
        lyricsButton.setAttribute("aria-pressed", String(on));
    }
    if (queueButton && kind !== "queue" && sheetOpen()) {
        queueButton.classList.remove("is-on");
        queueButton.setAttribute("aria-pressed", "false");
    }
}

/* Кнопки текста и очереди в плеере: на компьютере — прежнее поведение
 * (текст в середине, очередь в правой колонке), на телефоне — внутри
 * большого плеера. */
function playerLyrics() {
    if (WIDE.matches) { toggleLyricsView(); return; }
    if (!sheetOpen()) expandPlayer();
    showInSheet(sheetPanel === "lyrics" ? null : "lyrics");
}

function playerQueue() {
    if (WIDE.matches) { toggleQueuePanel(); return; }
    if (!sheetOpen()) expandPlayer();
    showInSheet(sheetPanel === "queue" ? null : "queue");
}

/* Повернули телефон или растянули окно до широкой раскладки — большой
 * плеер сворачивается, иначе он закрыл бы раскладку, где ему нет места. */
WIDE.addEventListener("change", () => { if (WIDE.matches) collapsePlayer(); });

/* Очередь опустела — сворачивать нечего показывать. */
new MutationObserver(() => {
    if (document.getElementById("player").hidden) collapsePlayer();
}).observe(document.getElementById("player"), { attributes: true, attributeFilter: ["hidden"] });

/* Жест: тянуть вниз за верх плеера (ручка, обложка, название). Лист идёт за
 * пальцем один к одному; отпустили ниже 140 точек или с размаху — свернулся,
 * иначе вернулся на место. */
(function swipeToCollapse() {
    const bar = document.getElementById("player");
    let startY = 0, lastY = 0, lastT = 0, velocity = 0, dragging = false, swallowClick = false;
    let lastWall = 0;
    bar.addEventListener("pointerdown", (e) => {
        if (!sheetOpen() || e.button !== 0) return;
        if (!e.target.closest(".player-grab, .player-art, .player-track")) return;
        dragging = true;
        startY = lastY = e.clientY;
        lastT = e.timeStamp;
        velocity = 0;
    });
    bar.addEventListener("pointermove", (e) => {
        if (!dragging) return;
        const dy = Math.max(0, e.clientY - startY);
        if (dy > 6 && !bar.classList.contains("is-dragging")) {
            bar.classList.add("is-dragging");
            bar.setPointerCapture(e.pointerId);
        }
        velocity = (e.clientY - lastY) / Math.max(1, e.timeStamp - lastT);
        lastY = e.clientY;
        lastT = e.timeStamp;
        lastWall = performance.now();
        if (bar.classList.contains("is-dragging")) bar.style.transform = `translateY(${dy}px)`;
    });
    const release = () => {
        if (!dragging) return;
        dragging = false;
        const dragged = bar.classList.contains("is-dragging");
        bar.classList.remove("is-dragging");
        bar.style.transform = "";
        if (!dragged) return;
        /* Отпускание после перетаскивания браузер считает ещё и нажатием —
         * его гасим, но только это одно: click приходит в той же череде
         * событий, а таймер снимает запрет сразу после. */
        swallowClick = true;
        setTimeout(() => { swallowClick = false; }, 0);
        /* Размах — только если палец ещё двигался: задержали перед
         * отпусканием — значит не бросок. */
        const fling = performance.now() - lastWall < 80 && velocity > 0.6;
        if (lastY - startY > 140 || fling) collapsePlayer();
    };
    bar.addEventListener("pointerup", release);
    bar.addEventListener("pointercancel", release);
    /* Тянули — не нажатие: иначе отпускание на обложке открывало бы текст. */
    bar.addEventListener("click", (e) => {
        if (!swallowClick) return;
        swallowClick = false;
        e.stopPropagation();
        e.preventDefault();
    }, true);
})();

/* Esc сворачивает плеер, только если закрывать больше нечего. Слушаем на
 * перехвате и раньше меню (их обработчики добавляются позже): иначе меню
 * успевало закрыться само, и тот же Esc сворачивал ещё и плеер. */
document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !sheetOpen()) return;
    if (openMenu) return;
    const sleep = document.getElementById("playerSleepMenu");
    if (sleep && !sleep.hidden) {
        /* Меню таймера — не из общих меню, Esc его само не закрывало. */
        e.preventDefault();
        closeSleepMenu();
        document.getElementById("playerSleep").focus({ preventScroll: true });
        return;
    }
    const el = document.activeElement;
    if (el && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && el.type !== "range"))) return;
    collapsePlayer();
}, true);

/* ---------------- «⋯» плеера ---------------- */

function openPlayerMore(button) {
    const wasMine = openMenu && openMenu.button === button;
    closeTrackMenu();
    if (wasMine) return;
    const track = player.queue[player.index];

    const menu = document.createElement("div");
    menu.className = "row-menu more-menu player-more-menu";
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
    /* Трека со стороны нет ни в подборке, ни в фонотеке — показывать негде. */
    if (track && !isOutside(track)) {
        item("Показать, откуда играет", () => { collapsePlayer(); revealTrack(track); });
    }
    item(tintEnabled() ? "Цвет из обложки: выключить" : "Цвет из обложки: включить",
        () => setCoverTint(!tintEnabled()));
    document.body.appendChild(menu);

    const box = button.getBoundingClientRect();
    const width = menu.offsetWidth;
    menu.style.left = Math.round(Math.max(8, Math.min(box.right - width, window.innerWidth - width - 8))) + "px";
    /* Внизу экрана меню встаёт над кнопкой, вверху — под ней. */
    if (box.top > window.innerHeight / 2) {
        menu.style.top = "auto";
        menu.style.bottom = Math.round(window.innerHeight - box.top + 6) + "px";
    } else {
        menu.style.top = Math.round(box.bottom + 6) + "px";
    }
    button.setAttribute("aria-expanded", "true");
    openMenu = { menu, button };
    document.addEventListener("keydown", menuKeydown, true);
    document.addEventListener("pointerdown", menuPointerDown, true);
    menu.querySelector(".row-menu-item").focus();
}

/* ---------------- Левая панель на компьютере ---------------- */

/* Кнопка «сузить / развернуть». Ширину и её запоминание ведёт app.js
 * (applyRailWidth, saveRailWidth) — здесь только переключение между краями. */
function toggleRailSlim() {
    const app = document.querySelector(".app");
    if (!app) return;
    const now = parseInt(getComputedStyle(app).getPropertyValue("--rail-w"), 10) || RAIL_DEFAULT;
    /* Разворачиваем до той ширины, что была до сужения, — её могли
     * подобрать мышью. */
    let wide = RAIL_DEFAULT;
    try { wide = parseInt(localStorage.getItem("railWideWidth"), 10) || RAIL_DEFAULT; } catch (e) { /* по умолчанию */ }
    if (now >= RAIL_SLIM_AT) {
        try { localStorage.setItem("railWideWidth", String(now)); } catch (e) { /* приватное окно */ }
        saveRailWidth(applyRailWidth(RAIL_MIN));
    } else {
        saveRailWidth(applyRailWidth(wide));
    }
    syncRailToggle();
}

function syncRailToggle() {
    const app = document.querySelector(".app");
    const button = document.getElementById("railToggle");
    if (!app || !button) return;
    const slim = app.classList.contains("rail-slim");
    const label = slim ? "Развернуть панель" : "Сузить панель";
    button.setAttribute("aria-label", label);
    button.title = label;
}

/* Ширину меняют и мышью за край — подпись кнопки должна это знать. */
new MutationObserver(syncRailToggle).observe(document.querySelector(".app"), { attributes: true, attributeFilter: ["class"] });
syncRailToggle();

/* Пауза видна и по обложке большого плеера: она отступает. */
(function followPause() {
    const bar = document.getElementById("player");
    const sync = () => bar.classList.toggle("is-paused", player.audio.paused);
    player.audio.addEventListener("play", sync);
    player.audio.addEventListener("pause", sync);
    sync();
})();

applyTint();
