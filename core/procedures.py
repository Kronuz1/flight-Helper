"""Автоподбор процедур SID / STAR / Approach.

Логика:
  • SID    — фильтр по активной ВПП вылета, ранжирование по тому, насколько
             выходная точка процедуры направлена на пункт назначения.
  • STAR   — фильтр по активной ВПП посадки (или 'All'), ранжирование по тому,
             насколько точка входа направлена в сторону аэропорта вылета
             (т.е. откуда прилетает борт).
  • Approach — по активной ВПП посадки, предпочтение точным заходам (ILS→RNAV→…).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from core import router
from navdata import db
from navdata.db import Airport, Procedure

# Приоритет типов захода (меньше число — выше предпочтение)
_APPROACH_RANK = [
    ("ILS", 0), ("IGS", 0), ("GLS", 1), ("RNV", 2), ("GPS", 2),
    ("LOC", 3), ("LDA", 3), ("LBC", 3), ("VOR", 4), ("VDM", 4),
    ("TAC", 5), ("NDB", 6), ("NDM", 6),
]
_RWY_RE = re.compile(r"(\d{1,2}[LRC]?)$")
_RWY_PARSE = re.compile(r"^(\d{1,2})([LRC]?)$")


# --------------------------------------------------------------------------- #
#  Геометрия
# --------------------------------------------------------------------------- #
def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Начальный пеленг из точки 1 в точку 2, градусы 0..360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def angdiff(a: float, b: float) -> float:
    """Наименьшая разница углов 0..180."""
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d


# --------------------------------------------------------------------------- #
#  Сопоставление ВПП
# --------------------------------------------------------------------------- #
def runway_matches(proc_runways: str, active: str) -> bool:
    """Подходит ли процедура (атрибут Runways) активной полосе active ('09L')."""
    proc_runways = (proc_runways or "").strip()
    if proc_runways.upper() in ("ALL", ""):
        return True
    ma = _RWY_PARSE.match(active)
    if not ma:
        return proc_runways == active
    num_a, suf_a = ma.group(1).zfill(2), ma.group(2)
    for token in re.split(r"[,\s]+", proc_runways):
        if not token:
            continue
        if token == active:
            return True
        mt = _RWY_PARSE.match(token)
        if mt:
            num_t, suf_t = mt.group(1).zfill(2), mt.group(2)
            if num_t == num_a and (suf_t == "" or suf_t == suf_a):
                return True
    return False


def approach_runway(name: str) -> str:
    m = _RWY_RE.search(name)
    return m.group(1) if m else ""


def runways_with_approach(icao: str) -> set[str]:
    """Множество обозначений ВПП, для которых опубликован хотя бы один заход."""
    rwys = set()
    for p in db.get_procedures(icao, "APPROACH"):
        r = approach_runway(p.name)
        if r:
            rwys.add(r)
    return rwys


def approach_type_rank(name: str) -> int:
    for root, rank in _APPROACH_RANK:
        if name.upper().startswith(root):
            return rank
    return 9


# --------------------------------------------------------------------------- #
#  Выбор «значимой» точки процедуры (с ненулевыми координатами)
# --------------------------------------------------------------------------- #
def _first_fix(proc: Procedure):
    for w in db.get_waypoints(proc.id):
        if w.lat or w.lon:
            return w
    return None


def _last_fix(proc: Procedure):
    last = None
    for w in db.get_waypoints(proc.id):
        if w.lat or w.lon:
            last = w
    return last


# --------------------------------------------------------------------------- #
#  Результат подбора
# --------------------------------------------------------------------------- #
@dataclass
class Pick:
    procedure: Procedure
    reason: str  # пояснение, почему выбрана


# --------------------------------------------------------------------------- #
#  SID
# --------------------------------------------------------------------------- #
def select_sid(dep: Airport, active_rwy: str, dest: Airport | None) -> list[Pick]:
    sids = [p for p in db.get_procedures(dep.icao, "SID") if runway_matches(p.runways, active_rwy)]
    if not sids:
        return []
    if dest is None:
        return [Pick(p, "по активной ВПП") for p in sids]

    route_brg = bearing(dep.lat, dep.lon, dest.lat, dest.lon)
    g = router.graph()
    scored = []
    for p in sids:
        fix = _last_fix(p)
        if fix is None:
            scored.append((999.0, 999.0, False, p))
            continue
        exit_brg = bearing(dep.lat, dep.lon, fix.lat, fix.lon)
        ang = angdiff(route_brg, exit_brg)
        _, on_graph = g.resolve(fix.name, fix.lat, fix.lon)  # точка выхода на трассе?
        score = ang + (0.0 if on_graph else 60.0)             # штраф за выход не на трассу
        scored.append((score, ang, on_graph, p))
    scored.sort(key=lambda x: x[0])
    return [_sid_pick(p, ang, on_graph) for _s, ang, on_graph, p in scored]


def _sid_pick(p: Procedure, ang: float, on_graph: bool) -> Pick:
    if ang >= 900:
        return Pick(p, "по активной ВПП")
    tail = ", на трассе" if on_graph else ", DCT-выход"
    return Pick(p, f"выход в сторону маршрута (Δ{ang:.0f}°){tail}")


# --------------------------------------------------------------------------- #
#  STAR
# --------------------------------------------------------------------------- #
def select_star(dest: Airport, active_rwy: str, dep: Airport | None) -> list[Pick]:
    stars = [p for p in db.get_procedures(dest.icao, "STAR") if runway_matches(p.runways, active_rwy)]
    if not stars:
        return []
    if dep is None:
        return [Pick(p, "по активной ВПП") for p in stars]

    inbound_brg = bearing(dest.lat, dest.lon, dep.lat, dep.lon)  # откуда прилетаем
    g = router.graph()
    scored = []
    for p in stars:
        fix = _first_fix(p)
        if fix is None:
            scored.append((999.0, 999.0, False, p))
            continue
        entry_brg = bearing(dest.lat, dest.lon, fix.lat, fix.lon)
        ang = angdiff(inbound_brg, entry_brg)
        _, on_graph = g.resolve(fix.name, fix.lat, fix.lon)  # точка входа на трассе?
        score = ang + (0.0 if on_graph else 60.0)
        scored.append((score, ang, on_graph, p))
    scored.sort(key=lambda x: x[0])
    return [_star_pick(p, ang, on_graph) for _s, ang, on_graph, p in scored]


def _star_pick(p: Procedure, ang: float, on_graph: bool) -> Pick:
    if ang >= 900:
        return Pick(p, "по активной ВПП")
    tail = ", на трассе" if on_graph else ", DCT-вход"
    return Pick(p, f"вход со стороны вылета (Δ{ang:.0f}°){tail}")


# --------------------------------------------------------------------------- #
#  Approach
# --------------------------------------------------------------------------- #
def select_approach(dest: Airport, active_rwy: str) -> list[Pick]:
    apps = [
        p for p in db.get_procedures(dest.icao, "APPROACH")
        if runway_matches(approach_runway(p.name), active_rwy)
    ]
    apps.sort(key=lambda p: (approach_type_rank(p.name), p.name))
    return [Pick(p, f"заход на ВПП {active_rwy}") for p in apps]
