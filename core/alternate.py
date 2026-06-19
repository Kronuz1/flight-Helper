"""Подбор запасного аэродрома.

Если в пункте назначения плохая погода (категория IFR/LIFR, сильный ветер
или низкая видимость) — ищем ближайший подходящий аэропорт с приемлемой
погодой: достаточная длина ВПП и категория не хуже MVFR.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from core import weather
from navdata import db
from navdata.db import Airport

# Параметры подбора
MIN_RUNWAY_FT = 6500        # минимальная длина ВПП запасного (под реактивные ВС)
SEARCH_RADIUS_KM = 400      # радиус поиска
CANDIDATE_LIMIT = 12        # сколько ближайших проверять по погоде
BAD_CATEGORIES = {"IFR", "LIFR"}
GOOD_CATEGORIES = {"VFR", "MVFR"}


def _km(a: Airport, b: Airport) -> float:
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = math.radians(b.lat - a.lat)
    dl = math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def is_bad_weather(m: weather.Metar | None) -> bool:
    if m is None:
        return False
    if m.flight_category in BAD_CATEGORIES:
        return True
    gust = m.wind_gust or 0
    if max(m.wind_speed, gust) >= 35:  # штормовой ветер
        return True
    return False


def weather_ok(m: weather.Metar | None) -> bool:
    if m is None:
        return False
    if m.flight_category and m.flight_category not in GOOD_CATEGORIES:
        return False
    if is_bad_weather(m):
        return False
    return True


@dataclass
class Alternate:
    airport: Airport
    distance_km: int
    metar: weather.Metar | None


def _candidates(dest: Airport) -> list[tuple[Airport, float]]:
    lengths = db.max_runway_lengths()
    out = []
    for ap in db.all_airports():
        if ap.icao == dest.icao:
            continue
        if lengths.get(ap.icao, 0) < MIN_RUNWAY_FT:
            continue
        d = _km(dest, ap)
        if d <= SEARCH_RADIUS_KM:
            out.append((ap, d))
    out.sort(key=lambda x: x[1])
    return out[:CANDIDATE_LIMIT]


async def find_alternate(dest: Airport) -> Alternate | None:
    """Ближайший подходящий аэропорт с приемлемой погодой."""
    cands = _candidates(dest)
    if not cands:
        return None
    metars = await weather.fetch_metars([ap.icao for ap, _ in cands])
    for ap, dist in cands:  # уже отсортированы по близости
        m = metars.get(ap.icao)
        if weather_ok(m):
            return Alternate(ap, int(round(dist)), m)
    # никто не подошёл по погоде — вернём ближайший (хоть какой-то вариант)
    ap, dist = cands[0]
    return Alternate(ap, int(round(dist)), metars.get(ap.icao))
