"""Справочник воздушных судов и полётные расчёты (эшелон, время, топливо).

Профили — типовые крейсерские значения для коммерческих джетов. Расчёты
упрощённые (без ветра, усреднённый крейсерский расход), достаточные для
симуляторного планирования: дать пилоту эшелон, время в пути и block fuel.
"""
from __future__ import annotations

from dataclasses import dataclass

# Константы расчёта топлива (общие для всех типов)
TAXI_KG = 250            # руление (вылет + прилёт), кг
CONTINGENCY = 0.05       # непредвиденный остаток, доля от trip fuel
FINAL_RESERVE_H = 0.5    # конечный резерв, часы крейсерского расхода (≈30 мин)


@dataclass(frozen=True)
class Aircraft:
    code: str           # ICAO-тип, напр. 'A20N'
    name: str           # человекочитаемое имя
    cruise_tas: int     # крейсерская истинная скорость, узлы
    cruise_fl: int      # типовой крейсерский эшелон (напр. 360 = FL360)
    fuel_flow: int      # суммарный крейсерский расход, кг/ч
    min_runway_ft: int  # минимальная потребная длина ВПП, фт


# Порядок задаёт порядок кнопок в боте.
_FLEET: list[Aircraft] = [
    Aircraft("A320", "Airbus A320 (CFM56)",        447, 360, 2500, 5500),
    Aircraft("A20N", "Airbus A320-251N (LEAP-1A26)", 455, 370, 2200, 6000),
    Aircraft("B738", "Boeing 737-800",             453, 370, 2600, 6000),
    Aircraft("E190", "Embraer 190",                447, 370, 1300, 4500),
    Aircraft("A359", "Airbus A350-900",            488, 370, 6000, 8200),
    Aircraft("B788", "Boeing 787-8",               488, 370, 5400, 8000),
    Aircraft("B77W", "Boeing 777-300ER",           490, 350, 7400, 9000),
    Aircraft("A388", "Airbus A380-800",            490, 370, 11000, 9800),
]

_BY_CODE: dict[str, Aircraft] = {a.code: a for a in _FLEET}


def get(code: str) -> Aircraft | None:
    return _BY_CODE.get((code or "").strip().upper())


def all_aircraft() -> list[Aircraft]:
    return list(_FLEET)


def all_codes() -> list[str]:
    return [a.code for a in _FLEET]


# --------------------------------------------------------------------------- #
#  Эшелон — правило полукруга (ИКАО)
# --------------------------------------------------------------------------- #
def cruise_fl_semicircular(track_deg: float, base_fl: int) -> int:
    """Допустимый крейсерский эшелон по путевому курсу.

    Курс 000–179° → нечётные тысячи футов (FL310/330/350…),
    курс 180–359° → чётные тысячи (FL320/340/360…).
    Возвращается ближайший к типовому base_fl эшелон нужной чётности.
    """
    east = (track_deg % 360) < 180          # восток → нечётные тысячи
    th = round(base_fl / 10)                # тысячи футов (FL360 → 36)
    if (th % 2 == 1) == east:
        best = th
    else:
        best = th + 1                       # сдвиг к ближайшей нужной чётности (вверх)
    return best * 10


# --------------------------------------------------------------------------- #
#  Время и топливо
# --------------------------------------------------------------------------- #
def ete_hours(distance_nm: float, ac: Aircraft) -> float:
    """Время в пути (часы), still-air: дистанция / крейсерская скорость."""
    if ac.cruise_tas <= 0:
        return 0.0
    return distance_nm / ac.cruise_tas


def fuel_estimate(dist_nm: float, alt_nm: float, ac: Aircraft) -> dict[str, int]:
    """Грубая оценка топлива (кг): trip / taxi / contingency / alternate / reserve / block."""
    ff = ac.fuel_flow
    trip = ff * ete_hours(dist_nm, ac)
    alternate = ff * ete_hours(alt_nm, ac) if alt_nm and alt_nm > 0 else 0.0
    contingency = CONTINGENCY * trip
    reserve = ff * FINAL_RESERVE_H
    block = trip + TAXI_KG + contingency + alternate + reserve
    return {
        "trip": int(round(trip)),
        "taxi": TAXI_KG,
        "contingency": int(round(contingency)),
        "alternate": int(round(alternate)),
        "reserve": int(round(reserve)),
        "block": int(round(block)),
    }


def format_hm(hours: float) -> str:
    """Часы → «2 ч 35 мин» / «47 мин»."""
    total = int(round(hours * 60))
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} ч {m:02d} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин"
