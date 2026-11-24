/* «Продолжить на другом устройстве» (сервер — adder/handoff.py).
 *
 * Каждое устройство раз в HANDOFF_EVERY_MS, пока играет, и на паузе/уходе со
 * страницы сообщает службе трек и место. Открыл другое устройство — снизу
 * плашка «Играло на ноутбуке: трек, 1:23 · Продолжить». Нажатие ставит ту же
 * очередь с того же места, а первое устройство на следующем своём отчёте
 * получает «yield» и встаёт на паузу — как в Spotify.
 *
 * «Чьё сейчас» решает последнее нажатие «играть»: claim — play после паузы
 * дольше HANDOFF_CLAIM_GAP_MS (или первое за сеанс). Переход к следующему
 * треку сам по себе не claim: иначе ноутбук, которому сказали остановиться,
 * успевал бы перейти к следующему треку и забрать музыку обратно.
 *
 * Очередь целиком (бывает вся фонотека) уходит только когда сменилась её
 * метка queueStamp (player.js, saveQueue).
 */

const HANDOFF_EVERY_MS = 10000;
const HANDOFF_CLAIM_GAP_MS = 1500;
/* Старше — уже не «продолжить», а история. */
const HANDOFF_FRESH_S = 12 * 3600;
const HANDOFF_DEVICE_KEY = "handoffDevice";
const HANDOFF_DISMISS_KEY = "handoffDismissed";

const handoff = {
    sentStamp: -1,     // метка очереди, отправленной целиком
    lastSent: 0,
    lastPlayingAt: 0,  // когда последний раз видели «играет»
    claimed: false,    // этот сеанс уже нажимал «играть»
    claimDue: false,   // «слушают здесь» ещё не дошло до службы
    again: false,      // пока шёл отчёт, понадобился ещё один
    offer: null,       // {now, saved}: что предлагает плашка
    busy: false,
    advancing: false,  // трек доиграл сам — следующий play не «нажали»
    device: "",
};

function handoffDevice() {
    if (handoff.device) return handoff.device;
    let id = "";
    try { id = localStorage.getItem(HANDOFF_DEVICE_KEY) || ""; } catch (e) { /* закрыто */ }
    if (id.length < 8) {
        id = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now());
        try { localStorage.setItem(HANDOFF_DEVICE_KEY, id); } catch (e) { /* без памяти — свой на сеанс */ }
    }
    /* В памяти страницы: при закрытом хранилище (приватный режим Safari)
     * каждый отчёт иначе шёл бы от «нового устройства» и ставил бы паузу сам себе. */
    handoff.device = id;
    return id;
}

/* Где играет — в предложном падеже, для «Играло на …». */
function handoffLabel() {
    const ua = navigator.userAgent || "";
    if (/iPhone|iPod/.test(ua)) return "айфоне";
    if (/iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) return "айпаде";
    if (/Android/.test(ua)) return "телефоне";
    return "ноутбуке";
}

function handoffPosition() {
    const a = player.audio;
    if (a.getAttribute("src") && !player.switching && Number.isFinite(a.currentTime)) return a.currentTime;
    return player.resumeAt || 0;
}

async function handoffReport() {
    if (!token()) return;
    /* Отчёт уже в пути — следующий сразу после него, а не никогда: потерянное
     * «слушают здесь» вернуло бы этому устройству паузу от службы. */
    if (handoff.busy) { handoff.again = true; return; }
    const track = player.queue[player.index];
    if (!track || !track.path) return;
    /* Свежая очередь в localStorage — она же уйдёт на сервер. */
    if (typeof saveQueue === "function") saveQueue();
    let queue;
    /* С заявкой «слушают здесь» — очередь всегда: на сервере могла остаться
     * очередь другого устройства, и без неё место не сохранить. */
    if (queueStamp !== handoff.sentStamp || handoff.claimDue) {
        try { queue = JSON.parse(localStorage.getItem(QUEUE_KEY) || "null") || undefined; } catch (e) { queue = undefined; }
        if (queue && queue.stamp !== queueStamp) queue = undefined;
    }
    const body = {
        device: handoffDevice(),
        label: handoffLabel(),
        track: {
            path: track.path,
            artist: String(track.artist || ""),
            title: String(track.title || ""),
            duration: Number.isFinite(player.audio.duration) && player.audio.duration > 0
                ? Math.min(86400, player.audio.duration) : null,
        },
        index: player.index,
        at: Math.max(0, Math.min(86400, handoffPosition())),
        playing: !audioPaused(),
        claim: handoff.claimDue,
        stamp: queueStamp,
    };
    if (queue) body.queue = queue;
    const json = JSON.stringify(body);
    handoff.busy = true;
    handoff.lastSent = Date.now();
    try {
        const r = await fetch("/api/now", {
            method: "PUT",
            headers: { ...headers(), "Content-Type": "application/json" },
            body: json,
            /* Уходя со страницы — дослать. keepalive не берёт больше 64 КиБ,
             * а кириллица — два байта на букву: считать байты, не буквы. */
            keepalive: new TextEncoder().encode(json).length < 60000,
            /* Зависший запрос не должен держать отчёты навсегда (busy). */
            signal: AbortSignal.timeout ? AbortSignal.timeout(15000) : undefined,
        });
        if (!r.ok) return;
        const answer = await r.json();
        if (queue) handoff.sentStamp = body.stamp;
        if (answer.need_queue) handoff.sentStamp = -1;
        else if (body.claim) handoff.claimDue = false;
        if (answer.yield && !audioPaused() && !handoff.claimDue) {
            player.pauseReason = "продолжили на другом устройстве";
            player.audio.pause();
            setPlayerNote("Продолжили на " + (answer.to || "другом устройстве"));
        }
    } catch (e) { /* без сети — следующий отчёт (claimDue остаётся) */ } finally {
        handoff.busy = false;
        if (handoff.again) { handoff.again = false; handoffReport(); }
    }
}

onAudio("ended", () => { handoff.advancing = true; });
onAudio("play", () => {
    const gap = Date.now() - handoff.lastPlayingAt;
    /* Трек доиграл и пошёл следующий — это не «нажали играть», даже если
     * следующий грузился дольше HANDOFF_CLAIM_GAP_MS. */
    const advanced = handoff.advancing;
    handoff.advancing = false;
    const claim = !handoff.claimed || (gap > HANDOFF_CLAIM_GAP_MS && !advanced);
    handoff.claimed = true;
    handoff.lastPlayingAt = Date.now();
    hideHandoffOffer();
    if (claim) { handoff.claimDue = true; handoffReport(); }
});
onAudio("timeupdate", () => {
    if (!audioPaused()) handoff.lastPlayingAt = Date.now();
});
onAudio("pause", () => { handoffReport(); });
setInterval(() => {
    if (!audioPaused() && Date.now() - handoff.lastSent >= HANDOFF_EVERY_MS - 500) handoffReport();
}, HANDOFF_EVERY_MS);
document.addEventListener("visibilitychange", () => {
    if (document.hidden) handoffReport();
    else checkHandoffOffer();
});

/* ---------------- Плашка «Продолжить» ---------------- */

function handoffDismissKey(now) {
    return `${now.device}|${now.stamp}|${now.track && now.track.path}`;
}

async function checkHandoffOffer() {
    if (!token() || !audioPaused()) return;
    let now = null;
    try {
        const r = await fetch("/api/now", { headers: headers() });
        if (!r.ok) return;
        now = (await r.json()).now;
    } catch (e) { return; }
    if (!now || !now.track || now.device === handoffDevice()) { hideHandoffOffer(); return; }
    /* Возраст и место считает сервер — по своим часам (adder/handoff.py). */
    if (!(now.age >= 0 && now.age < HANDOFF_FRESH_S)) return;
    try { if (sessionStorage.getItem(HANDOFF_DISMISS_KEY) === handoffDismissKey(now)) return; } catch (e) { /* нет */ }
    const at = now.at || 0;
    /* Здесь уже то же самое и там же — продолжать нечего. */
    const mine = player.queue[player.index];
    if (mine && mine.path === now.track.path && Math.abs(handoffPosition() - at) < 15) return;
    /* Очередь — заранее: на айфоне play() разрешён только сразу после нажатия,
     * запрос по сети между нажатием и play() его бы съел. */
    let saved = null;
    try {
        const r = await fetch("/api/now/queue?stamp=" + encodeURIComponent(now.stamp), { headers: headers() });
        if (r.ok) saved = await r.json();
    } catch (e) { return; }
    if (!saved || !Array.isArray(saved.queue) || !saved.queue.length) return;
    if (!audioPaused()) return;  // пока спрашивали, включили своё
    handoff.offer = { now, saved, at };
    showHandoffOffer(now, at);
}

function showHandoffOffer(now, at) {
    const bar = document.getElementById("handoffBar");
    if (!bar) return;
    const names = trackNames(now.track);
    const where = now.label || "другом устройстве";
    document.getElementById("handoffWhere").textContent =
        (now.playing ? "Играет на " : "Играло на ") + where + (at > 5 ? " · " + formatTime(at) : "");
    document.getElementById("handoffTrack").textContent =
        [names.title, names.artist].filter(Boolean).join(" — ");
    bar.hidden = false;
}

function hideHandoffOffer() {
    const bar = document.getElementById("handoffBar");
    if (bar) bar.hidden = true;
    handoff.offer = null;
}

function dismissHandoffOffer() {
    const offer = handoff.offer;
    if (offer) {
        try { sessionStorage.setItem(HANDOFF_DISMISS_KEY, handoffDismissKey(offer.now)); } catch (e) { /* нет */ }
    }
    hideHandoffOffer();
}

function takeHandoff() {
    const offer = handoff.offer;
    if (!offer) return;
    hideHandoffOffer();
    const { now, saved, at } = offer;
    /* Место — из последнего отчёта: очередь могла уйти вперёд без перемены метки. */
    let index = Number.isInteger(now.index) ? now.index : saved.index;
    if (!saved.queue[index] || saved.queue[index].path !== now.track.path) {
        index = saved.queue.findIndex(t => t && t.path === now.track.path);
    }
    if (index < 0) return;
    /* Сразу, без ожидания: play() ниже должен остаться в том же нажатии. */
    fetch("/api/now/take", {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify({ device: handoffDevice(), label: handoffLabel() }),
    }).catch(() => {});
    const a = player.audio;
    a.pause();
    a.removeAttribute("src");
    a.load();
    if (!applySavedQueue({ ...saved, index, at })) return;
    if (typeof saveQueueSoon === "function") saveQueueSoon(true);
    togglePlay();
}

document.addEventListener("DOMContentLoaded", () => { setTimeout(checkHandoffOffer, 1500); });
