"""Маршрутизация по воздушным трассам (airways).

Строит граф из таблицы airways: узлы — точки трасс, рёбра — сегменты между
соседними точками одной трассы. Поиск пути A* минимизирует длину, оставаясь
на трассах; при разрывах допускаются короткие DCT-перемычки (со штрафом),
что соответствует реальным маршрутам (… SOPAS DCT LENIR …).

Граф строится один раз и кэшируется на время работы процесса.
"""
from __future__ import annotations

import heapq
import math
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache

from navdata import db

_NM = 1852.0  # метр в морской миле — для перевода


def _gc_nm(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Дистанция по большому кругу в морских милях."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000.0 * 2 * math.asin(math.sqrt(h)) / _NM


def great_circle_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Публичная дистанция по большому кругу (морские мили)."""
    return _gc_nm((lat1, lon1), (lat2, lon2))


@lru_cache(maxsize=None)
def airway_level(name: str) -> str:
    """Классификация трассы по обозначению: 'U' верхняя, 'L' нижняя, 'N' прочая.

    Эвристика по первой букве ИКАО-обозначения: U… (UL/UM/UN/UP…), J (jet),
    Q (high RNAV) — верхние; V (Victor) и T (US T-routes) — нижние; остальные
    (A/B/G/H/L/M/N/P/R/W/Y/Z…) региональные — нейтральные.
    """
    n = (name or "").upper()
    if not n:
        return "N"
    if n[0] == "U" or n[0] in ("J", "Q"):
        return "U"
    if n[0] in ("V", "T"):
        return "L"
    return "N"


# --------------------------------------------------------------------------- #
#  Граф трасс (singleton)
# --------------------------------------------------------------------------- #
class AirwayGraph:
    def __init__(self):
        self.pos: list[tuple[float, float]] = []
        self.ident: list[str] = []
        self.adj: list[list[tuple[int, str, float]]] = []  # (neighbor, airway, dist_nm)
        self.by_ident: dict[str, list[int]] = defaultdict(list)
        self.grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        self._key2id: dict[tuple, int] = {}

    def _node(self, ident: str, lat: float, lon: float) -> int:
        key = (ident, round(lat, 2), round(lon, 2))
        i = self._key2id.get(key)
        if i is None:
            i = len(self.pos)
            self._key2id[key] = i
            self.pos.append((lat, lon))
            self.ident.append(ident)
            self.adj.append([])
            self.by_ident[ident].append(i)
            self.grid[(math.floor(lat), math.floor(lon))].append(i)
        return i

    def build(self):
        rows = db.load_airways()
        prev = None  # (airway, node_id, lat, lon)
        for awy, seq, ident, lat, lon in rows:
            nid = self._node(ident, lat, lon)
            if prev and prev[0] == awy:
                d = _gc_nm((prev[2], prev[3]), (lat, lon))
                self.adj[prev[1]].append((nid, awy, d))
                self.adj[nid].append((prev[1], awy, d))
            prev = (awy, nid, lat, lon)
        return self

    # ближайшие узлы для DCT-перемычек (ленивая генерация при поиске)
    def dct_neighbors(self, i: int, radius_nm: float = 45.0, limit: int = 5):
        lat, lon = self.pos[i]
        cl, co = math.floor(lat), math.floor(lon)
        out = []
        airway_nbrs = {n for n, _, _ in self.adj[i]}
        for dla in (-1, 0, 1):
            for dlo in (-2, -1, 0, 1, 2):
                for j in self.grid.get((cl + dla, co + dlo), ()):
                    if j == i or j in airway_nbrs:
                        continue
                    d = _gc_nm((lat, lon), self.pos[j])
                    if d <= radius_nm:
                        out.append((d, j))
        out.sort()
        return out[:limit]

    def resolve(self, ident: str, lat: float, lon: float) -> tuple[int | None, bool]:
        """Узел графа для точки. Возвращает (id, on_graph)."""
        best, bd = None, 1e9
        for j in self.by_ident.get(ident, ()):
            d = _gc_nm((lat, lon), self.pos[j])
            if d < bd:
                bd, best = d, j
        if best is not None and bd <= 5.0:
            return best, True
        # точка не на трассе — ищем ближайший узел расширяющимся поиском по сетке
        cl, co = math.floor(lat), math.floor(lon)
        for ring in range(1, 8):
            cand, cd = None, 1e9
            for dla in range(-ring, ring + 1):
                for dlo in range(-2 * ring, 2 * ring + 1):
                    for j in self.grid.get((cl + dla, co + dlo), ()):
                        d = _gc_nm((lat, lon), self.pos[j])
                        if d < cd:
                            cd, cand = d, j
            if cand is not None:
                return cand, False
        return None, False


_graph: AirwayGraph | None = None


def graph() -> AirwayGraph:
    global _graph
    if _graph is None:
        _graph = AirwayGraph().build()
    return _graph


# --------------------------------------------------------------------------- #
#  A* поиск
# --------------------------------------------------------------------------- #
def _astar(g: AirwayGraph, start: int, goal: int, max_expand: int = 200_000):
    goal_pos = g.pos[goal]
    came: dict[int, tuple[int, str]] = {}
    gscore = {start: 0.0}
    h0 = _gc_nm(g.pos[start], goal_pos)
    frontier = [(h0, 0.0, start)]
    visited = set()
    expanded = 0
    while frontier:
        _, gc, cur = heapq.heappop(frontier)
        if cur == goal:
            return came
        if cur in visited:
            continue
        visited.add(cur)
        expanded += 1
        if expanded > max_expand:
            return None
        # рёбра трасс (нижние трассы — со штрафом, чтобы джет шёл верхними)
        for nbr, awy, d in g.adj[cur]:
            step = d * 1.5 + 5.0 if airway_level(awy) == "L" else d
            ng = gc + step
            if ng < gscore.get(nbr, 1e18):
                gscore[nbr] = ng
                came[nbr] = (cur, awy)
                f = ng + _gc_nm(g.pos[nbr], goal_pos)
                heapq.heappush(frontier, (f, ng, nbr))
        # DCT-перемычки (повышенный штраф, чтобы держаться опубликованных трасс)
        for d, nbr in g.dct_neighbors(cur):
            ng = gc + d * 1.6 + 20.0
            if ng < gscore.get(nbr, 1e18):
                gscore[nbr] = ng
                came[nbr] = (cur, "DCT")
                f = ng + _gc_nm(g.pos[nbr], goal_pos)
                heapq.heappush(frontier, (f, ng, nbr))
    return None


def _reconstruct(came, start, goal):
    """Список рёбер (airway, to_ident_id) от start к goal."""
    legs = []
    cur = goal
    while cur != start:
        if cur not in came:
            return None
        prev, awy = came[cur]
        legs.append((awy, cur))
        cur = prev
    legs.reverse()
    return legs


# --------------------------------------------------------------------------- #
#  Публичный интерфейс
# --------------------------------------------------------------------------- #
@dataclass
class Fix:
    ident: str
    lat: float
    lon: float


def route_with_distance(start: Fix, goal: Fix) -> tuple[list[str], float]:
    """Токены маршрута (FIX AIRWAY FIX …) и его геометрическая длина в милях."""
    g = graph()
    s_id, s_on = g.resolve(start.ident, start.lat, start.lon)
    e_id, e_on = g.resolve(goal.ident, goal.lat, goal.lon)

    fallback = (
        [start.ident, "DCT", goal.ident],
        great_circle_nm(start.lat, start.lon, goal.lat, goal.lon),
    )
    if s_id is None or e_id is None:
        return fallback

    came = _astar(g, s_id, e_id)
    if came is None:
        return fallback
    legs = _reconstruct(came, s_id, e_id)
    if legs is None:
        return fallback

    # сборка токенов со схлопыванием участков одной трассы
    tokens: list[str] = [g.ident[s_id]]
    j = 0
    while j < len(legs):
        awy, node = legs[j]
        if awy == "DCT":
            tokens += ["DCT", g.ident[node]]
            j += 1
        else:
            k = j
            while k + 1 < len(legs) and legs[k + 1][0] == awy:
                k += 1
            tokens += [awy, g.ident[legs[k][1]]]
            j = k + 1

    # геометрическая длина пути по узлам графа (+ DCT-вход/выход при off-graph точках)
    node_seq = [s_id] + [node for _, node in legs]
    dist = sum(
        _gc_nm(g.pos[node_seq[i - 1]], g.pos[node_seq[i]])
        for i in range(1, len(node_seq))
    )

    # если точки не лежат на трассах — добавляем DCT-вход/выход с реальной точкой
    if not s_on and start.ident != tokens[0]:
        tokens = [start.ident, "DCT"] + tokens
        dist += great_circle_nm(start.lat, start.lon, *g.pos[s_id])
    if not e_on and goal.ident != tokens[-1]:
        tokens = tokens + ["DCT", goal.ident]
        dist += great_circle_nm(goal.lat, goal.lon, *g.pos[e_id])
    return tokens, dist


def route_between(start: Fix, goal: Fix) -> list[str]:
    """Список токенов маршрута: FIX AIRWAY FIX AIRWAY ... (с DCT при разрывах)."""
    return route_with_distance(start, goal)[0]
