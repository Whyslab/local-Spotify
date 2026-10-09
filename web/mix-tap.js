/* «Слух» перехода «как диджей» (mix.js): узел Web Audio, который копирует
 * проходящий через него звук в главный поток — кусками по 1024 кадра, с
 * номером первого кадра на часах AudioContext. По этому номеру mix.js знает,
 * в какой момент прозвучал каждый отсчёт. Пока не попросили, молчит. */
class MixTap extends AudioWorkletProcessor {
    constructor() {
        super();
        this.on = false;
        this.buf = new Float32Array(1024);
        this.fill = 0;
        this.first = 0;
        this.port.onmessage = (event) => {
            this.on = event.data === true;
            this.fill = 0;
        };
    }

    process(inputs) {
        if (!this.on) return true;
        const input = inputs[0];
        const n = input.length ? input[0].length : 128;
        if (this.fill === 0) this.first = currentFrame;
        for (let i = 0; i < n; i++) {
            let sum = 0;
            for (let c = 0; c < input.length; c++) sum += input[c][i];
            this.buf[this.fill + i] = input.length ? sum / input.length : 0;
        }
        this.fill += n;
        if (this.fill >= this.buf.length) {
            this.port.postMessage({ frame: this.first, data: this.buf.slice(0, this.fill) });
            this.fill = 0;
        }
        return true;
    }
}

registerProcessor("mix-tap", MixTap);
