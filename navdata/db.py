"""Доступ к навигационной базе (SQLite), собранной importer'ом."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from functools import lru_cache

import config


@dataclass
class Airport:
    icao: str
    name: str
    lat: float
    lon: float


@dataclass
class Runway:
    icao: str
    ident: str
    length_ft: int
    heading: int  # магнитный курс
    lat: float
    lon: float


@dataclass
class Procedure:
    id: int
    icao: str
    kind: str  # SID | STAR | APPROACH
    name: str
    runways: str


@dataclass
class Waypoint:
    seq: int
    name: str
    wtype: str
    lat: float
    lon: float
    altitude: int
    alt_restriction: str
    speed: int


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@lru_cache(maxsize=1)
def airac_info() -> dict[str, str]:
    with _conn() as c:
        rows = c.execute("SELECT key, value FROM meta").fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_airport(icao: str) -> Airport | None:
    icao = icao.strip().upper()
    with _conn() as c:
        r = c.execute("SELECT * FROM airports WHERE icao=?", (icao,)).fetchone()
    return Airport(r["icao"], r["name"], r["lat"], r["lon"]) if r else None


def get_runways(icao: str) -> list[Runway]:
    icao = icao.strip().upper()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM runways WHERE icao=? ORDER BY ident", (icao,)
        ).fetchall()
    return [
        Runway(r["icao"], r["ident"], r["length_ft"], r["heading"], r["lat"], r["lon"])
        for r in rows
    ]


def get_procedures(icao: str, kind: str | None = None) -> list[Procedure]:
    icao = icao.strip().upper()
    sql = "SELECT * FROM procedures WHERE icao=?"
    params: list = [icao]
    if kind:
        sql += " AND kind=?"
        params.append(kind.upper())
    sql += " ORDER BY name"
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [
        Procedure(r["id"], r["icao"], r["kind"], r["name"], r["runways"]) for r in rows
    ]


def get_waypoints(proc_id: int) -> list[Waypoint]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM procedure_waypoints WHERE proc_id=? ORDER BY seq", (proc_id,)
        ).fetchall()
    return [
        Waypoint(
            r["seq"],
            r["name"],
            r["wtype"],
            r["lat"],
            r["lon"],
            r["altitude"],
            r["alt_restriction"],
            r["speed"],
        )
        for r in rows
    ]


def airport_exists(icao: str) -> bool:
    return get_airport(icao) is not None


def load_airways() -> list[tuple[str, int, str, float, float]]:
    """Все точки трасс: (airway, seq, ident, lat, lon), упорядоченные."""
    with _conn() as c:
        rows = c.execute(
            "SELECT airway, seq, ident, lat, lon FROM airways ORDER BY airway, seq"
        ).fetchall()
    return [(r["airway"], r["seq"], r["ident"], r["lat"], r["lon"]) for r in rows]


def max_runway_lengths() -> dict[str, int]:
    """ICAO -> длина самой длинной ВПП (фт). Для фильтра запасных аэродромов."""
    with _conn() as c:
        rows = c.execute(
            "SELECT icao, MAX(length_ft) AS m FROM runways GROUP BY icao"
        ).fetchall()
    return {r["icao"]: r["m"] for r in rows}


def all_airports() -> list[Airport]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM airports").fetchall()
    return [Airport(r["icao"], r["name"], r["lat"], r["lon"]) for r in rows]
