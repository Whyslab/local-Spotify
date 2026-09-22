"""Тематические подборки из того, что уже измерено.

Ни одного нового источника: темп, энергия и яркость каждого трека посчитаны
скриптом анализа и лежат в `audio_features`. Здесь они только раскладываются
по настроениям.

**Границы берутся от самой фонотеки, а не из воздуха.** «Спокойное» при
абсолютном пороге вроде «темп ниже 95» дало бы на этой фонотеке 18 треков
из 1109 — подборку, которую не стоит показывать. Четверти же существуют
всегда: самая спокойная четверть есть у любого собрания музыки, даже если
вся она быстрая. Поэтому подборка описывает не абсолютное настроение,
а место трека среди остальных.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Mood:
    key: str
    name: str
    hint: str
    paths: list[str]


def _quantile(values: list[float], part: float) -> float:
    """Значение, ниже которого лежит `part` измеренных треков."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(len(ordered) * part)))
    return ordered[index]


def collections(rows: list[dict], limit: int = 50, seed: int | None = None) -> list[Mood]:
    """Подборки по настроению из измеренных треков.

    `rows` — записи `audio_features`: path, tempo, energy, brightness.
    Треки без измерений просто не участвуют: соврать про их настроение
    хуже, чем не показать их в подборке.
    """
    measured = [r for r in rows if r.get("tempo") is not None and r.get("energy") is not None]
    if len(measured) < 20:
        # Меньше двадцати — четверти перестают что-либо значить.
        return []

    tempos = [float(r["tempo"]) for r in measured]
    energies = [float(r["energy"]) for r in measured]
    brights = [float(r["brightness"]) for r in measured if r.get("brightness") is not None]

    tempo_mid, tempo_high = _quantile(tempos, 0.5), _quantile(tempos, 0.75)
    energy_low, energy_mid = _quantile(energies, 0.25), _quantile(energies, 0.5)
    bright_high = _quantile(brights, 0.75) if brights else None

    def pick(test, key, name, hint) -> Mood | None:
        chosen = [r["path"] for r in measured if test(r)]
        if len(chosen) < 8:
            return None
        rng = random.Random(seed if seed is not None else len(chosen))
        rng.shuffle(chosen)
        return Mood(key, name, hint, chosen[:limit])

    wanted = [
        pick(
            lambda r: float(r["tempo"]) > tempo_mid and float(r["energy"]) > energy_mid,
            "car",
            "В машину",
            "быстрее и громче половины фонотеки",
        ),
        pick(
            lambda r: float(r["tempo"]) >= tempo_high,
            "run",
            "Разогнаться",
            "самая быстрая четверть",
        ),
        pick(
            lambda r: float(r["energy"]) <= energy_low,
            "work",
            "На работу",
            "самая тихая четверть — не тянет на себя внимание",
        ),
    ]
    if bright_high is not None:
        wanted.append(
            pick(
                lambda r: r.get("brightness") is not None and float(r["brightness"]) >= bright_high,
                "bright",
                "Поярче",
                "звонкое и высокое",
            )
        )
    return [mood for mood in wanted if mood is not None]
