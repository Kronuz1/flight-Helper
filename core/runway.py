"""Выбор активной ВПП по ветру.

Принцип: для каждой ВПП считаем встречную (headwind) и боковую (crosswind)
составляющие ветра. Активной считается полоса с максимальной встречной
составляющей. При штиле/переменном ветре — самая длинная полоса (разумный
выбор по умолчанию для лайнера).

Замечание о точности: направление ветра в METAR отсчитывается от ИСТИННОГО
севера, а курс ВПП в навбазе — магнитный. Для ВЫБОРА полосы это не важно
(полоса и её реверс отличаются на 180°, склонение влияет на обе одинаково),
поэтому магнитным склонением пренебрегаем — это стандартная практика для
симуляторных планировщиков.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from core.weather import Metar
from navdata.db import Runway


@dataclass
class RunwayWind:
    runway: Runway
    headwind: float  # узлы, >0 встречный, <0 попутный
    crosswind: float  # узлы, абсолютная величина боковой составляющей
    crosswind_side: str  # 'L' | 'R' | ''  — с какой стороны дует


def _components(runway_hdg: int, wind_dir: int, wind_speed: int) -> tuple[float, float, str]:
    angle = math.radians(wind_dir - runway_hdg)
    headwind = wind_speed * math.cos(angle)
    cross = wind_speed * math.sin(angle)
    side = "R" if cross > 0 else ("L" if cross < 0 else "")
    return headwind, abs(cross), side


def rank_runways(runways: list[Runway], metar: Metar | None) -> list[RunwayWind]:
    """Список ВПП, отсортированный по предпочтительности (лучшая — первая)."""
    if not runways:
        return []

    calm = (
        metar is None
        or metar.wind_variable
        or metar.wind_dir is None
        or metar.wind_speed <= 3
    )

    if calm:
        # штиль/переменный — ранжируем по длине полосы (длиннее = лучше)
        ranked = [
            RunwayWind(rw, headwind=0.0, crosswind=0.0, crosswind_side="")
            for rw in sorted(runways, key=lambda r: r.length_ft, reverse=True)
        ]
        return ranked

    result = []
    for rw in runways:
        hw, cw, side = _components(rw.heading, metar.wind_dir, metar.wind_speed)
        result.append(RunwayWind(rw, headwind=hw, crosswind=cw, crosswind_side=side))
    # лучшая = максимальный встречный ветер; при равенстве — длиннее полоса
    result.sort(key=lambda x: (x.headwind, x.runway.length_ft), reverse=True)
    return result


def active_runway(runways: list[Runway], metar: Metar | None) -> RunwayWind | None:
    ranked = rank_runways(runways, metar)
    return ranked[0] if ranked else None


def format_runway_choice(rw: RunwayWind, calm: bool) -> str:
    r = rw.runway
    base = f"<b>{r.ident}</b> (курс {r.heading:03d}°, {r.length_ft} фт)"
    if calm:
        return base + " — штиль, выбор по длине ВПП"
    parts = []
    if rw.headwind >= 0:
        parts.append(f"встречный {rw.headwind:.0f} уз")
    else:
        parts.append(f"попутный {abs(rw.headwind):.0f} уз")
    if rw.crosswind >= 1:
        parts.append(f"боковой {rw.crosswind:.0f} уз {rw.crosswind_side}")
    return base + " — " + ", ".join(parts)
