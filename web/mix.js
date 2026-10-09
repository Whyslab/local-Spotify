/* ---------------- Переход «как диджей» (только ноутбук) ----------------
 *
 * Треки по-прежнему играет <audio>. На стыке звук ненадолго берёт связка —
 * короткий кусок, сведённый службой (scripts/render_transition.py): чистый
 * конец трека A, сам переход, чистое начало трека B. Порядок:
 *
 *   1. Пока A играет, плеер просит у службы связку A → B и расшифровывает её.
 *   2. Когда A доходит до начала связки, плеер «слушает» A секунду и ищет
 *      услышанное в начале связки (взаимная корреляция). Так он знает, какой
 *      отсчёт связки звучит в какой кадр часов AudioContext, — до отсчёта.
 *   3. Связка запускается на этих часах точно в такт с A и за 30 мс сменяет
 *      его (A продолжает играть без звука — по нему идёт полоска времени).
 *   4. Ближе к концу связки элемент колоды с треком B пускается без звука,
 *      плеер слушает его и подводит под конец связки (перемоткой и скоростью),
 *      пока расхождение не станет меньше половины миллисекунды.
 *   5. Связка за 30 мс уступает элементу B, и он становится текущим треком.
 *
 * Почему по звуку, а не по currentTime: в окне (WebKitGTK) currentTime
 * элемента, подключённого к Web Audio, врёт на секунду (проверено 09.10);
 * звук же сверяется с образцом до отсчёта в обоих движках.
 *
 * Всё это — через Web Audio, поэтому только там, где странице подчиняется
 * громкость (не iPhone: там Web Audio обрывает фоновое воспроизведение, и
 * граф даже не создаётся). Где перехода не вышло — обычное затухание 3 с.
 * Громкость в графе ведут узлы: окно не слушает volume и muted элемента,
 * подключённого к Web Audio (проверено 09.10), — см. mixVolume.
 */

const MIX_LISTEN = 1.2;      // с: столько слушать A (две половины — для сверки)
const MIX_SWAP = 0.03;       // с: длина замены одного на другое (звук тот же)
const MIX_MATCH = 0.8;       // нижняя граница сходства услышанного с образцом
const MIX_DOCK_TOLERANCE = 0.0005;  // с: B «встал», если расходится меньше
const MIX_SEEK_ABOVE = 0.012;  // с: дальше — перемотать B, ближе — подогнать связку
const MIX_SLEW = 0.005;        // доля скорости связки при подгонке

const mix = {
    ctx: null,
    ready: null,       // Promise<boolean>: слух (mix-tap.js) загружен
    silent: null,      // общий узел-заглушка, через который слух тянет звук
    nodes: new WeakMap(),  // <audio> → {source, gain, tap, held}
    prep: null,        // связка для текущей пары: {from, to, toIndex, status, plan, buffer, ref}
    run: null,         // идущий переход
    last: null,        // как прошёл последний (для журнала и тестов)
};

class MixError extends Error {}

function mixEnabled() {
    return Boolean(player.djMode && player.volumeAdjustable
        && typeof AudioContext === "function" && typeof AudioWorkletNode === "function");
}

/* Контекст создаётся только из действий человека (кнопка, включение трека):
 * без этого браузер его не запустит. */
function mixContext() {
    if (!mixEnabled()) return null;
    if (!mix.ctx) {
        try {
            mix.ctx = new AudioContext({ latencyHint: "playback" });
        } catch (e) {
            return null;
        }
        mix.ready = mix.ctx.audioWorklet.addModule("/static/mix-tap.js").then(() => true, () => false);
        mix.silent = mix.ctx.createGain();
        mix.silent.gain.value = 0;
        mix.silent.connect(mix.ctx.destination);
        mix.ctx.addEventListener("statechange", () => {
            /* Подключённый элемент звучит только через контекст: остановился
             * контекст — замолчала музыка. Пробуем поднять и пишем в журнал. */
            if (mix.ctx.state !== "running" && mix.nodes.has(player.audio) && !player.audio.paused) {
                mix.ctx.resume().catch(() => {});
                playerEvent("mix-fail", "звук Web Audio остановлен: " + mix.ctx.state);
            }
        });
    }
    if (mix.ctx.state === "suspended") mix.ctx.resume().catch(() => {});
    return mix.ctx;
}

/* Подключить элемент к графу — до того, как он заиграет: на ходу подключение
 * дало бы щелчок. Обратно не отключить: подключённый звучит только так. */
function mixAttach(el) {
    if (mix.nodes.has(el)) return true;
    const ctx = mixContext();
    if (!ctx || ctx.state !== "running") return false;
    try {
        const source = ctx.createMediaElementSource(el);
        const gain = ctx.createGain();
        source.connect(gain).connect(ctx.destination);
        mix.nodes.set(el, { source, gain, tap: null, held: false });
        el.volume = 1;
        return true;
    } catch (e) {
        return false;
    }
}

/* Включили «как диджей» кнопкой: подключить то, что играет сейчас. */
function mixEnable() {
    if (mixAttach(player.audio)) applyVolume();
}

function bridgeLevel() {
    return player.audio.muted ? 0 : player.userVolume * Math.min(1, sleepLevel());
}

/* Громкость подключённого элемента — его узлом (см. начало файла). Пока идёт
 * замена, узлы ведёт переход (held). */
function mixVolume(el, level) {
    const n = mix.nodes.get(el);
    if (!n) return false;
    el.volume = 1;
    const now = mix.ctx.currentTime;
    if (!n.held) n.gain.gain.setValueAtTime(el.muted ? 0 : level, now);
    const run = mix.run;
    if (run && run.bridgeGain && !run.bridgeHeld) run.bridgeGain.gain.setValueAtTime(bridgeLevel(), now);
    return true;
}

/* ---------------- Подготовка: какая пара и её связка ---------------- */

function mixPair() {
    const current = player.queue[player.index];
    const at = peekNext();
    const next = at >= 0 ? player.queue[at] : null;
    if (!current || !next || at === player.index) return null;
    if (isOutside(current) || isOutside(next)) return null;
    if (player.repeat === "one" || (player.sleep && player.sleep.track)) return null;
    return { from: current.path, to: next.path, toIndex: at };
}

function mixWindow(plan) {
    // Слушать A можно, пока до конца чистого A в связке хватает времени на всё.
    return { from: plan.a_from + 0.3, to: plan.a_from + plan.lead - 2.4 };
}

/* Зовётся из timeupdate текущего трека. */
function mixTick() {
    if (!mixEnabled() || !mix.ctx || mix.ctx.state !== "running" || mix.run) return;
    const a = player.audio;
    if (!mix.nodes.has(a)) return;  // трек начался без графа — этот стык обычный
    const pair = mixPair();
    if (!pair) { mix.prep = null; return; }
    let prep = mix.prep;
    if (!prep || prep.from !== pair.from || prep.to !== pair.to) {
        prep = mix.prep = { ...pair, status: "new", retryAt: 0, errors: 0 };
    }
    if ((prep.status === "new" || prep.status === "pending") && !prep.busy && Date.now() >= prep.retryAt) {
        mixAsk(prep);
    }
    if (prep.status !== "ready" || a.paused || a.seeking) return;
    const w = mixWindow(prep.plan);
    if (a.currentTime >= w.from && a.currentTime <= w.to) mixStart(prep);
}

async function mixAsk(prep) {
    prep.busy = true;
    try {
        const r = await fetch("/api/transitions", {
            method: "POST",
            headers: { ...headers(), "Content-Type": "application/json" },
            body: JSON.stringify({ from: prep.from, to: prep.to }),
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const answer = await r.json();
        if (mix.prep !== prep) return;
        if (answer.status === "pending") {
            prep.status = "pending";
            prep.retryAt = Date.now() + 4000;
            return;
        }
        if (answer.status !== "ready") {
            prep.status = "none";
            prep.reason = answer.reason || "";
            return;
        }
        const audio = await fetch(`/api/transitions/${answer.key}.flac`, { headers: headers() });
        if (!audio.ok) throw new Error("HTTP " + audio.status);
        const buffer = await mix.ctx.decodeAudioData(await audio.arrayBuffer());
        if (mix.prep !== prep) return;
        prep.plan = answer.plan;
        prep.buffer = buffer;
        prep.ref = monoOf(buffer);
        prep.status = "ready";
    } catch (e) {
        if (mix.prep !== prep) return;
        prep.errors += 1;
        prep.status = prep.errors >= 3 ? "none" : "pending";
        prep.retryAt = Date.now() + 15000;
    } finally {
        prep.busy = false;
    }
}

function monoOf(buffer) {
    const out = new Float32Array(buffer.length);
    for (let c = 0; c < buffer.numberOfChannels; c++) {
        const data = buffer.getChannelData(c);
        for (let i = 0; i < out.length; i++) out[i] += data[i] / buffer.numberOfChannels;
    }
    return out;
}

/* Конец трека — забота перехода: обычное затухание не нужно, пока переход
 * идёт или ещё успеет начаться. */
function mixHoldsEnd() {
    if (mix.run) return true;
    const prep = mix.prep;
    const current = player.queue[player.index];
    if (!prep || prep.status !== "ready" || !current || prep.from !== current.path) return false;
    return player.audio.currentTime <= mixWindow(prep.plan).to;
}

/* Звучит связка, а A, доиграв без звука, уже «на паузе» — для кнопок и
 * экрана это всё ещё игра. */
function mixSounding() {
    return Boolean(mix.run && mix.run.phase !== "listen");
}

/* Конец A во время перехода — не повод листать очередь: следующий трек
 * подведёт сам переход. */
function mixOwnsEnd() {
    return Boolean(mix.run);
}

/* ---------------- Слух ---------------- */

async function mixTap(n) {
    if (n.tap) return n.tap;
    if (!(await mix.ready)) throw new MixError("нет слуха (AudioWorklet)");
    const tap = new AudioWorkletNode(mix.ctx, "mix-tap");
    n.source.connect(tap);
    tap.connect(mix.silent);
    n.tap = tap;
    return tap;
}

/* Записать seconds того, что идёт через tap; {frame — кадр первого отсчёта, data}. */
function record(tap, seconds) {
    const need = Math.round(seconds * mix.ctx.sampleRate);
    return new Promise((resolve, reject) => {
        const out = new Float32Array(need);
        let got = 0;
        let first = null;
        let expect = null;
        const stop = () => { tap.port.onmessage = null; tap.port.postMessage(false); };
        const timer = setTimeout(() => { stop(); reject(new MixError("слух молчит")); }, (seconds + 3) * 1000);
        tap.port.onmessage = (event) => {
            const { frame, data } = event.data;
            if (expect !== null && frame !== expect) { got = 0; first = null; }  // пропуск — заново
            if (first === null) first = frame;
            expect = frame + data.length;
            const take = Math.min(data.length, need - got);
            out.set(data.subarray(0, take), got);
            got += take;
            if (got >= need) {
                clearTimeout(timer);
                stop();
                resolve({ frame: first, data: out });
            }
        };
        tap.port.postMessage(true);
    });
}

function fft(re, im, inverse) {
    const n = re.length;
    for (let i = 1, j = 0; i < n; i++) {
        let bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            let t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t;
        }
    }
    for (let len = 2; len <= n; len <<= 1) {
        const angle = (2 * Math.PI / len) * (inverse ? 1 : -1);
        const wr = Math.cos(angle);
        const wi = Math.sin(angle);
        const half = len >> 1;
        for (let i = 0; i < n; i += len) {
            let cr = 1;
            let ci = 0;
            for (let k = 0; k < half; k++) {
                const a = i + k;
                const b = a + half;
                const tr = re[b] * cr - im[b] * ci;
                const ti = re[b] * ci + im[b] * cr;
                re[b] = re[a] - tr;
                im[b] = im[a] - ti;
                re[a] += tr;
                im[a] += ti;
                const nr = cr * wr - ci * wi;
                ci = cr * wi + ci * wr;
                cr = nr;
            }
        }
    }
}

/* Где в ref лучше всего лежит rec: {at — индекс начала, score — сходство 0…1}. */
function locate(rec, ref) {
    if (ref.length < rec.length) return { at: 0, score: 0 };
    let n = 1;
    while (n < ref.length + rec.length) n <<= 1;
    const ar = new Float64Array(n);
    const ai = new Float64Array(n);
    const br = new Float64Array(n);
    const bi = new Float64Array(n);
    ar.set(ref);
    br.set(rec);
    fft(ar, ai, false);
    fft(br, bi, false);
    for (let i = 0; i < n; i++) {
        const r = ar[i] * br[i] + ai[i] * bi[i];
        const m = ai[i] * br[i] - ar[i] * bi[i];
        ar[i] = r;
        ai[i] = m;
    }
    fft(ar, ai, true);
    let at = 0;
    let best = -Infinity;
    for (let k = 0; k <= ref.length - rec.length; k++) {
        if (ar[k] > best) { best = ar[k]; at = k; }
    }
    let dot = 0, er = 0, ef = 0;
    for (let i = 0; i < rec.length; i++) {
        dot += rec[i] * ref[at + i];
        er += rec[i] * rec[i];
        ef += ref[at + i] * ref[at + i];
    }
    return { at, score: er > 0 && ef > 0 ? dot / Math.sqrt(er * ef) : 0 };
}

/* Подождать, пока часы контекста дойдут до t. */
async function ctxWait(t) {
    while (mix.ctx.currentTime < t) {
        await new Promise(r => setTimeout(r, Math.max(10, Math.min(250, (t - mix.ctx.currentTime) * 1000))));
    }
}

function check(run) {
    if (mix.run !== run || run.cancelled) throw new MixError("отменён");
}

/* «Без звука» — кнопка плеера, она у текущего элемента. */
function levelOf(gain) {
    return player.audio.muted ? 0 : player.userVolume * gainFactor(gain) * Math.min(1, sleepLevel());
}

/* ---------------- Переход ---------------- */

async function mixStart(prep) {
    const ctx = mix.ctx;
    const sr = ctx.sampleRate;
    const plan = prep.plan;
    const a = player.audio;
    const run = mix.run = { prep, a, phase: "listen", cancelled: false };
    try {
        const nA = mix.nodes.get(a);
        const tap = await mixTap(nA);
        // 1. Где сейчас A в начале связки: по двум половинам — и сверить их.
        // Слушать по кругу: currentTime говорит, где A у декодера, а не что звучит.
        // В WebKit звук доходит до графа на секунду позже — первый круг бывает до связки.
        const half = Math.round(MIX_LISTEN / 2 * sr);
        const lead = prep.ref.subarray(0, Math.round(plan.lead * sr));
        let rec, one, two, said, saidAt;
        for (;;) {
            said = a.currentTime;
            saidAt = ctx.currentTime;
            rec = await record(tap, MIX_LISTEN);
            check(run);
            one = locate(rec.data.subarray(0, half), lead);
            two = locate(rec.data.subarray(half), lead);
            if (one.score >= MIX_MATCH && two.score >= MIX_MATCH) break;
            if (a.currentTime >= plan.a_from + plan.lead) {
                throw new MixError(`A не узнан (${one.score.toFixed(2)}/${two.score.toFixed(2)})`);
            }
        }
        // Кадр часов, в который прозвучал бы нулевой отсчёт связки.
        const zeroOne = rec.frame - one.at;
        const zeroTwo = rec.frame + half - two.at;
        if (Math.abs(zeroOne - zeroTwo) > sr * 0.001) {
            throw new MixError(`A плывёт: ${((zeroOne - zeroTwo) / sr * 1000).toFixed(1)} мс`);
        }
        run.zero = zeroTwo / sr;
        // На сколько currentTime опережает звук: у B будет так же.
        run.lag = Math.min(3, Math.max(0, said - saidAt - plan.a_from + run.zero));
        check(run);

        // 2. Связка — в такт с A, сначала без звука; потом замена.
        const when = ctx.currentTime + 0.12;
        const swap = when + 0.05;
        if (swap - run.zero > plan.lead - 0.3) throw new MixError("не успели до начала перехода");
        const src = ctx.createBufferSource();
        src.buffer = prep.buffer;
        const gain = ctx.createGain();
        gain.gain.value = 0;
        src.connect(gain).connect(ctx.destination);
        src.start(when, when - run.zero);
        run.src = src;
        run.bridgeGain = gain;
        run.bridgeHeld = true;
        nA.held = true;
        nA.gain.gain.cancelScheduledValues(0);
        nA.gain.gain.setValueAtTime(levelOf(player.trackGain), swap);
        nA.gain.gain.linearRampToValueAtTime(0, swap + MIX_SWAP);
        gain.gain.setValueAtTime(0, swap);
        gain.gain.linearRampToValueAtTime(bridgeLevel(), swap + MIX_SWAP);
        run.phase = "bridge";
        await ctxWait(swap + MIX_SWAP + 0.01);
        check(run);
        run.bridgeHeld = false;
        gain.gain.setValueAtTime(bridgeLevel(), ctx.currentTime);

        // 3. Трек B под конец связки.
        await mixDock(run);
    } catch (e) {
        if (mix.run === run) mixCancel(e);
    }
}

async function mixDock(run) {
    const ctx = mix.ctx;
    const sr = ctx.sampleRate;
    const { prep } = run;
    const plan = prep.plan;
    const track = player.queue[prep.toIndex];
    if (!track || track.path !== prep.to) throw new MixError("очередь изменилась");
    const stream = takePrefetched(track) || await streamUrlFor(track.path);
    check(run);
    const b = spareDeck();
    run.b = b;
    run.stream = stream;
    // В граф до play: иначе B прозвучит мимо связки.
    if (!mixAttach(b)) throw new MixError("B не подключить");
    const nB = mix.nodes.get(b);
    nB.held = true;
    nB.gain.gain.cancelScheduledValues(0);
    nB.gain.gain.setValueAtTime(0, ctx.currentTime);
    b.src = stream.url;
    b.playbackRate = 1;

    // Пустить B заранее — на время, где связка уже чистый B (с поправкой на запуск).
    await ctxWait(run.zero + plan.b_solo_at - 1.0);
    check(run);
    // Где B должен быть сейчас по часам; lead — поправка на запуск и перемотку,
    // её уточняет каждый замер. Опоздание звука у A (run.lag) сюда не входит:
    // после перемотки WebKit отдаёт звук сразу, отстаёт только currentTime.
    let lead = 0;
    const seekB = () => { b.currentTime = Math.max(0, plan.b_from + (ctx.currentTime - run.zero - plan.b_solo_at) + lead); };
    seekB();
    await b.play();
    check(run);
    const tap = await mixTap(nB);
    await ctxWait(run.zero + plan.b_solo_at + 0.1);
    check(run);

    const regionStart = Math.round((plan.b_solo_at + 0.03) * sr);  // первые 30 мс — шов
    const deadline = run.zero + plan.length - 1.0;
    let delta = null;
    while (ctx.currentTime < deadline) {
        const rec = await record(tap, 0.5);
        check(run);
        // Связка в кадр rec.frame — на отсчёте (rec.frame - zero); ищем B рядом (±1,5 с).
        const expected = rec.frame - Math.round(run.zero * sr);
        const lo = Math.max(regionStart, expected - Math.round(1.5 * sr));
        const hi = Math.min(prep.ref.length, expected + rec.data.length + Math.round(1.5 * sr));
        const found = locate(rec.data, prep.ref.subarray(lo, hi));
        if (found.score < MIX_MATCH) {
            // Не узнали — B мог ещё не выйти на место: подтянуть грубо и снова.
            seekB();
            await ctxWait(ctx.currentTime + 0.3);
            continue;
        }
        delta = lo + found.at - expected;  // > 0: B впереди связки, отсчёты
        (run.trace = run.trace || []).push([+(delta / sr * 1000).toFixed(2), +found.score.toFixed(3)]);
        if (Math.abs(delta) <= sr * MIX_DOCK_TOLERANCE) break;
        const shift = delta / sr;  // > 0: B впереди — связке догонять
        if (Math.abs(shift) > MIX_SEEK_ABOVE) {
            // Крупно — перемоткой B (звука у B пока нет, её не слышно). Мимо она
            // бывает и после: в Chromium на кусок буфера (21 мс), в WebKit до ±15 мс.
            lead -= shift;
            seekB();
            await ctxWait(ctx.currentTime + 0.3);
        } else {
            // Мелко — подогнать связку: её скорость Web Audio держит до отсчёта, а у
            // элемента — нет (Chromium меняет её рывками, с задержкой в полсекунды).
            // Не быстрее MIX_SLEW: 0,5 % — меньше девяти центов, на слух незаметно.
            const span = Math.max(0.3, Math.abs(shift) / MIX_SLEW);
            const t0 = ctx.currentTime + 0.02;
            run.src.playbackRate.setValueAtTime(1 + shift / span, t0);
            run.src.playbackRate.setValueAtTime(1, t0 + span);
            await ctxWait(t0 + span + 0.02);
            run.zero -= shift;  // связка ушла вперёд на shift
        }
        check(run);
    }
    check(run);
    const docked = delta !== null && Math.abs(delta) <= sr * MIX_DOCK_TOLERANCE;
    // Не встал — всё равно отдать звук B: тишина хуже. Замена длиннее, чтобы сгладить.
    const swapLength = docked ? MIX_SWAP : 0.3;
    const at = ctx.currentTime + 0.03;
    run.bridgeHeld = true;
    run.bridgeGain.gain.cancelScheduledValues(0);
    run.bridgeGain.gain.setValueAtTime(bridgeLevel(), at);
    run.bridgeGain.gain.linearRampToValueAtTime(0, at + swapLength);
    nB.gain.gain.setValueAtTime(0, at);
    nB.gain.gain.linearRampToValueAtTime(levelOf(stream.gain), at + swapLength);
    await ctxWait(at + swapLength + 0.01);
    check(run);
    mixAdopt(run, docked, delta === null ? null : delta / sr);
}

/* B становится текущим — как после playAt, только без нового источника. */
function mixAdopt(run, docked, delta) {
    const { prep, a, b, stream } = run;
    reportPlay(true);
    const next = stepInOrder(1);
    mixRelease(run);
    if (next < 0 || !player.queue[next] || player.queue[next].path !== prep.to) {
        // Очередь поменяли посреди перехода: честнее включить то, что теперь следующее.
        b.pause();
        if (next >= 0) playAt(next);
        return;
    }
    swapDeck(b);
    a.pause();
    a.removeAttribute("src");
    a.load();
    player.generation += 1;
    player.index = next;
    player.reported = false;
    player.started = true;
    player.playingMode = player.queueMode;
    player.trackGain = stream.gain;
    player.fadeLevel = Math.min(1, sleepLevel());
    player.mixAdopted = true;
    applyVolume();
    trackStarted();
    for (const name of ["loadedmetadata", "durationchange", "play", "playing", "timeupdate"]) {
        b.dispatchEvent(new Event(name));
    }
    const ms = delta === null ? "?" : (delta * 1000).toFixed(2);
    mix.last = { case: prep.plan.case, docked, delta, lag: run.lag, trace: run.trace || [] };
    playerEvent(docked ? "mix" : "mix-fail", `${prep.plan.case}, стык ${ms} мс`);
}

/* Убрать связку и отпустить узлы (переход кончился или отменён). */
function mixRelease(run) {
    run.cancelled = true;
    if (mix.run === run) mix.run = null;
    // Эта пара больше не пробуется: после отмены — обычное затухание.
    run.prep.status = "done";
    if (run.src) {
        try { run.src.stop(); } catch (e) { /* уже стоит */ }
        run.src.disconnect();
    }
    for (const el of [run.a, run.b]) {
        const n = el && mix.nodes.get(el);
        if (n) { n.held = false; n.gain.gain.cancelScheduledValues(0); }
    }
}

/* Отмена: человек что-то нажал, или переход не вышел. До замены и пока A ещё
 * играет — назад к A (он шёл без звука на своём месте). A уже доиграл — к B,
 * туда, куда дошёл переход (follow; playAt зовёт без него — он сам включает
 * другой трек). */
function mixCancel(reason, follow = true) {
    const run = mix.run;
    if (!run) return;
    const at = run.zero === undefined ? 0 : mixPositionOfNext(run);
    mixRelease(run);
    if (run.b) {
        run.b.pause();
        run.b.removeAttribute("src");
        run.b.load();
        const n = mix.nodes.get(run.b);
        if (n) n.gain.gain.setValueAtTime(0, mix.ctx.currentTime);
    }
    if (reason instanceof Error && reason.message !== "отменён") {
        playerEvent("mix-fail", reason.message || String(reason));
    }
    if (run.a !== player.audio) return;
    applyVolume();
    if (follow && run.a.ended) mixToNext(at, false);
}

/* Где сейчас трек B по ходу связки (с): до чистого B — чуть раньше его начала. */
function mixPositionOfNext(run) {
    const plan = run.prep.plan;
    const t = mix.ctx.currentTime - run.zero;
    return Math.max(0, plan.b_from + (t - plan.b_solo_at));
}

function mixToNext(at, paused) {
    reportPlay(true);
    const next = stepInOrder(1);
    if (next >= 0) playAt(next, 0, 1, { startAt: at, paused });
    else renderPlayer();
}

/* «Пауза», пока звучит связка, а A уже доиграл: встать на B и не играть. */
function mixPauseOnNext() {
    const run = mix.run;
    if (!run) return;
    const at = mixPositionOfNext(run);
    mixCancel(null, false);
    mixToNext(at, true);
}

/* Подписки: шаг перехода — на каждом timeupdate; пауза, перемотка или сбой
 * уходящего трека — отмена (назад к нему). Пауза в самом конце трека — это
 * его конец, а не кнопка. */
onAudio("timeupdate", mixTick);
onAudio("pause", () => { if (mix.run && !player.audio.ended) mixCancel(null); });
onAudio("seeking", () => { if (mix.run) mixCancel(null); });
onAudio("error", () => { if (mix.run) mixCancel(new MixError("ошибка уходящего трека")); });
