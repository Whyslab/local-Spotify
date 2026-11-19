#!/usr/bin/env python3
"""Звучание каждого трека — для умного перемешивания по звуку (adder/sonic.py).

Темп и громкость не отличают инди-поп от трэпа: они могут совпасть до удара.
Здесь каждый трек проходит через нейросеть Essentia Discogs-EffNet (обучена
различать 400 стилей Discogs) и получает:

- эмбеддинг — 1280 чисел, среднее по всему треку; близкие по звуку треки
  дают близкие эмбеддинги;
- 400 стилей Discogs (для имени облака и отладки);
- «головы» поверх эмбеддинга: весёлое, грустное, агрессивное, спокойное,
  для вечеринки, танцевальность, голос или инструментал;
- возбуждение и валентность (arousal / valence, шкала 1–9) — через MusiCNN
  и голову DEAM: из них «волна энергии».

Модели не лежат в git: их лицензия CC BY-NC-SA, а репозиторий публичный.
Их скачивает ``scripts/fetch_models.py`` в ``adder/models/``.

Мел-спектр считается здесь, на numpy, точно как в Essentia
(TensorflowInputMusiCNN): 16 кГц, окно Ханна 512 (симметричное), шаг 256,
96 полос Slaney, мощность, log10(1 + 10000·x), первый кадр — по центру
нуля. Моно — среднее каналов, как в MonoLoader (``-ac 1`` у ffmpeg громче
в √2 раз, и эмбеддинг от этого уезжал). Сверено 19.11.2026 с
essentia-tensorflow на Champagne Squirt: косинус среднего эмбеддинга с
эталоном 0,99997 на том же сигнале и 0,9995 с собственным декодером Essentia.

Запускать в один поток и с nice — как ночной анализ темпа:

    systemd-run --user --scope -p MemoryMax=1500M \\
        nice -n 15 .venv/bin/python scripts/embed_audio.py

Продолжает с места остановки: трек с тем же содержимым (sha256) второй раз
не считается.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Один поток во всём, что умеет больше: машина занята другими службами.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

if TYPE_CHECKING:
    import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
# Соседний скрипт — по имени файла, как его видит mypy (files = scripts).
sys.path.insert(0, str(REPO / "scripts"))

from analyze_audio import file_hash, library_files, probe_duration  # noqa: E402

MODELS_DIR = REPO / "adder" / "models"

# Версия расчёта. Сменилась — все треки считаются заново.
VERSION = "effnet1"

SAMPLE_RATE = 16000
N_FFT = 512
HOP = 256
N_MELS = 96
# Длинные файлы (альбом одним роликом, 30 минут) — только середина. Шести
# минут хватает на звучание, а память и время не растут с длиной.
MAX_SECONDS = 360
EFFNET_PATCH, EFFNET_HOP = 128, 62
MUSICNN_PATCH, MUSICNN_HOP = 187, 93
# MusiCNN в onnxruntime медленный: 9 с на трек из 12 (замер 19.11 на
# ThinkPad). Возбуждению и валентности хватает 16 патчей по 3 с,
# разложенных по треку ровно: ошибка среднего ~0,1–0,2 по шкале 1–9 (8 патчей
# давали до 0,4), время ~1,2 с.
MUSICNN_MAX_PATCHES = 16
# Сколько патчей за раз: весь трек одной пачкой брал до 900 МБ.
BATCH = 32

EFFNET = "discogs-effnet-bsdynamic-1.onnx"
MUSICNN = "msd-musicnn-1.onnx"
DEAM = "deam-msd-musicnn-2.onnx"
# Голова → индекс нужного класса в её выходе (по JSON модели).
HEADS = {
    "happy": ("mood_happy-discogs-effnet-1.onnx", 0),
    "sad": ("mood_sad-discogs-effnet-1.onnx", 0),
    "aggressive": ("mood_aggressive-discogs-effnet-1.onnx", 0),
    "relaxed": ("mood_relaxed-discogs-effnet-1.onnx", 1),
    "party": ("mood_party-discogs-effnet-1.onnx", 1),
    "danceable": ("danceability-discogs-effnet-1.onnx", 0),
    "voice": ("voice_instrumental-discogs-effnet-1.onnx", 1),
}


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    from adder.db import SONIC_SCHEMA

    con.execute(SONIC_SCHEMA)
    con.commit()
    return con


def _mel_filters() -> np.ndarray:
    """Треугольные фильтры Slaney с нормой «площадь 1» — как MelBands Essentia."""
    import numpy as np

    f_sp = 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0

    def hz_to_mel(f: np.ndarray) -> np.ndarray:
        return np.where(
            f >= min_log_hz,
            min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep,
            f / f_sp,
        )

    def mel_to_hz(m: np.ndarray) -> np.ndarray:
        return np.where(
            m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), f_sp * m
        )

    fft_freqs = np.linspace(0, SAMPLE_RATE / 2, N_FFT // 2 + 1)
    edges = np.array([0.0, SAMPLE_RATE / 2])
    lo, hi = hz_to_mel(edges)
    points = mel_to_hz(np.linspace(lo, hi, N_MELS + 2))
    widths = np.diff(points)
    ramps = points[:, None] - fft_freqs[None, :]
    weights = np.zeros((N_MELS, len(fft_freqs)))
    for i in range(N_MELS):
        lower = -ramps[i] / widths[i]
        upper = ramps[i + 2] / widths[i + 1]
        weights[i] = np.maximum(0, np.minimum(lower, upper))
    weights *= (2.0 / (points[2 : N_MELS + 2] - points[:N_MELS]))[:, None]
    return weights


def mel_band_centre(band: int) -> float:
    """Частота середины полосы ``band`` (Гц) — для проверок."""
    import numpy as np

    filters = _mel_filters()
    freqs = np.linspace(0, SAMPLE_RATE / 2, N_FFT // 2 + 1)
    return float(freqs[int(np.argmax(filters[band]))])


def melspectrogram(signal: np.ndarray) -> np.ndarray:
    """Кадры × 96 полос, как TensorflowInputMusiCNN внутри Essentia."""
    import numpy as np

    # Essentia ставит центр первого кадра на нулевой отсчёт: полкадра нулей
    # спереди. Без них каждый патч сдвинут на кадр (косинус патча до 0,95).
    x = np.concatenate([np.zeros(N_FFT // 2), signal.astype(np.float64)])
    count = 1 + max(0, -(-(len(x) - N_FFT) // HOP)) if len(x) >= N_FFT else 1
    pad = (count - 1) * HOP + N_FFT - len(x)
    if pad > 0:
        x = np.concatenate([x, np.zeros(pad)])
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / (N_FFT - 1))
    filters = _mel_filters().T
    out = np.empty((count, N_MELS), dtype=np.float32)
    # Кусками: 15 000 кадров × 512 в float64 — это 60 МБ ради одной FFT.
    step = 2048
    for start in range(0, count, step):
        idx = np.arange(N_FFT)[None, :] + HOP * np.arange(start, min(count, start + step))[:, None]
        power = np.abs(np.fft.rfft(x[idx] * window, axis=1)) ** 2
        out[start : start + len(idx)] = np.log10(1 + 10000 * (power @ filters))
    return out


def patches(mel: np.ndarray, size: int, hop: int) -> np.ndarray:
    import numpy as np

    if len(mel) < size:
        # Короче одного патча (заставка в пару секунд): дополнить тишиной,
        # иначе у трека не будет звучания вовсе.
        mel = np.concatenate([mel, np.zeros((size - len(mel), N_MELS), dtype=mel.dtype)])
    return np.stack([mel[s : s + size] for s in range(0, len(mel) - size + 1, hop)])


def decode(path: Path) -> np.ndarray:
    """Середина трека (до MAX_SECONDS), 16 кГц, моно как среднее каналов."""
    import numpy as np

    total = probe_duration(path)
    offset = max(0.0, (total - MAX_SECONDS) / 2)
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-ss",
            f"{offset:.3f}",
            "-t",
            str(MAX_SECONDS),
            "-i",
            str(path),
            "-f",
            "f32le",
            "-ac",
            "2",
            "-ar",
            str(SAMPLE_RATE),
            "-",
        ],
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip()[-120:])
    stereo = np.frombuffer(result.stdout, dtype=np.float32)
    if stereo.size < 2:
        raise ValueError("no audio decoded")
    return stereo[: stereo.size // 2 * 2].reshape(-1, 2).mean(axis=1)


class Models:
    """Сессии onnxruntime, каждая в один поток."""

    def __init__(self, folder: Path = MODELS_DIR) -> None:
        import onnxruntime as ort

        missing = [
            name
            for name in [EFFNET, MUSICNN, DEAM, *(f for f, _ in HEADS.values())]
            if not (folder / name).exists()
        ]
        if missing:
            raise SystemExit(
                f"Нет моделей в {folder}: {', '.join(missing)}.\n"
                f"Скачать: .venv/bin/python scripts/fetch_models.py"
            )
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3

        def load(name: str) -> Any:
            return ort.InferenceSession(
                str(folder / name), options, providers=["CPUExecutionProvider"]
            )

        self.effnet = load(EFFNET)
        self.musicnn = load(MUSICNN)
        self.deam = load(DEAM)
        self.heads = {key: (load(name), index) for key, (name, index) in HEADS.items()}

    @staticmethod
    def run(session: Any, data: np.ndarray) -> dict[str, np.ndarray]:
        """Все выходы модели по именам, пачками по BATCH патчей."""
        import numpy as np

        name = session.get_inputs()[0].name
        outputs = [o.name for o in session.get_outputs()]
        parts: dict[str, list] = {o: [] for o in outputs}
        for i in range(0, len(data), BATCH):
            for key, value in zip(
                outputs, session.run(None, {name: data[i : i + BATCH]}), strict=True
            ):
                parts[key].append(value)
        return {key: np.concatenate(value) for key, value in parts.items()}


def measure(path: Path, models: Models) -> dict:
    """Всё, что пишется в строку таблицы ``sonic`` (кроме пути и хеша)."""
    import numpy as np

    mel = melspectrogram(decode(path))

    effnet = models.run(models.effnet, patches(mel, EFFNET_PATCH, EFFNET_HOP))
    per_patch, styles = effnet["embeddings"], effnet["activations"]

    # Головы Essentia применяет к каждому патчу и усредняет ответы, а не
    # к среднему эмбеддингу — так и здесь.
    heads = {}
    for key, (session, index) in models.heads.items():
        heads[key] = float(models.run(session, per_patch)["activations"][:, index].mean())

    musicnn_in = patches(mel, MUSICNN_PATCH, MUSICNN_HOP)
    if len(musicnn_in) > MUSICNN_MAX_PATCHES:
        keep = np.linspace(0, len(musicnn_in) - 1, MUSICNN_MAX_PATCHES).round().astype(int)
        musicnn_in = musicnn_in[keep]
    musicnn_emb = models.run(models.musicnn, musicnn_in)["embeddings"]
    (deam,) = models.run(models.deam, musicnn_emb).values()
    valence, arousal = deam.mean(axis=0)

    embedding = per_patch.mean(axis=0)
    embedding /= max(float(np.linalg.norm(embedding)), 1e-12)
    return {
        "embedding": embedding.astype(np.float32).tobytes(),
        "styles": styles.mean(axis=0).astype(np.float16).tobytes(),
        **{key: round(value, 4) for key, value in heads.items()},
        "arousal": round(float(arousal), 4),
        "valence": round(float(valence), 4),
    }


COLUMNS = (
    "embedding",
    "styles",
    "happy",
    "sad",
    "aggressive",
    "relaxed",
    "party",
    "danceable",
    "voice",
    "arousal",
    "valence",
)


def store(con: sqlite3.Connection, rel: str, digest: str, row: dict) -> None:
    names = ", ".join(COLUMNS)
    marks = ", ".join("?" for _ in COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in COLUMNS)
    con.execute(
        f"INSERT INTO sonic(path, sha256, version, {names}, analyzed_at) "
        f"VALUES(?, ?, ?, {marks}, datetime('now','localtime')) "
        f"ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, version=excluded.version, "
        f"{updates}, analyzed_at=excluded.analyzed_at",
        (rel, digest, VERSION, *(row[c] for c in COLUMNS)),
    )
    con.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=REPO / "adder" / "adder.db")
    parser.add_argument("--models", type=Path, default=MODELS_DIR)
    parser.add_argument("--limit", type=int, default=0, help="stop after N tracks")
    parser.add_argument("--only", type=Path, default=None, help="one file and stop")
    parser.add_argument(
        "--budget",
        type=float,
        default=0,
        help="stop after this many minutes (0 = no limit); the next run resumes",
    )
    args = parser.parse_args()

    os.environ.setdefault("API_TOKEN", "analysis")
    try:
        from adder import config, library
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"{exc.name} is not installed for {sys.executable}.\n"
            f"Run it with the project's interpreter: {REPO / '.venv/bin/python'}"
        ) from exc
    try:
        import onnxruntime  # noqa: F401
    except ModuleNotFoundError:
        raise SystemExit(
            "onnxruntime не установлен.\n"
            "    .venv/bin/pip install -r scripts/requirements-analysis.txt"
        ) from None

    root = (args.library or config.LIBRARY).resolve()
    con = connect(args.db)
    models = Models(args.models)

    targets = [args.only.resolve()] if args.only else library_files(root, library.AUDIO_SUFFIXES)
    known = {
        row[0]: (row[1], row[2])
        for row in con.execute("SELECT path, sha256, version FROM sonic").fetchall()
    }
    started = time.monotonic()
    done = skipped = failed = 0
    for path in targets:
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        if args.budget and time.monotonic() - started > args.budget * 60:
            print(f"budget of {args.budget:g} min spent; resuming next run", flush=True)
            break
        digest = file_hash(path)
        if known.get(rel) == (digest, VERSION):
            skipped += 1
            continue
        t0 = time.monotonic()
        try:
            row = measure(path, models)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {rel}: {str(exc)[:90]}", flush=True)
            continue
        store(con, rel, digest, row)
        done += 1
        print(f"  ✓ {rel} ({time.monotonic() - t0:.1f} s)", flush=True)
        if args.limit and done >= args.limit:
            break

    # Строки треков, которых больше нет на диске. Только при полном проходе:
    # --only и --limit видят не всю фонотеку.
    removed = 0
    if not args.only and not args.limit and not args.budget:
        present = set()
        for path in targets:
            try:
                present.add(str(path.relative_to(root)))
            except ValueError:
                present.add(str(path))
        gone = [p for p in known if p not in present]
        if targets and len(gone) < len(known) * 0.5:
            # Пустая или наполовину пропавшая фонотека — скорее диск не
            # смонтирован, чем полфонотеки удалили: тогда ничего не трогаем.
            con.executemany("DELETE FROM sonic WHERE path = ?", [(p,) for p in gone])
            con.commit()
            removed = len(gone)

    elapsed = time.monotonic() - started
    print(
        f"embedded {done}, unchanged {skipped}, failed {failed}, removed {removed} "
        f"in {elapsed / 60:.1f} min",
        flush=True,
    )
    return 1 if failed and not done else 0


if __name__ == "__main__":
    raise SystemExit(main())
