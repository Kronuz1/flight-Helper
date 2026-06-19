"""Импорт навданных Navigraph (native DFD) из zip-архива в SQLite.

Запуск (разово, при обновлении AIRAC):
    python -m navdata.importer
    python -m navdata.importer "C:\\path\\to\\navdata_native_XXXX.zip"

Архив содержит:
  airports.dat   — ICAO + координаты аэропорта
  wpnavapt.txt   — ВПП: имя/ICAO/обозначение/длина(фт)/маг.курс/координаты порога
  wpnavfix.txt   — точки маршрута (fix)
  wpnavaid.txt   — навигационные средства (VOR/NDB/DME)
  cycle_info.txt — сведения об AIRAC-цикле
  {icao}.xml     — терминальные процедуры SID/STAR/Approach с waypoint'ами
"""
from __future__ import annotations

import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import config

# Знаковый float с десятичной точкой — для координат
FLOAT_RE = re.compile(r"-?\d+\.\d+")


# --------------------------------------------------------------------------- #
#  Схема БД
# --------------------------------------------------------------------------- #
SCHEMA = """
DROP TABLE IF EXISTS meta;
DROP TABLE IF EXISTS airports;
DROP TABLE IF EXISTS runways;
DROP TABLE IF EXISTS navaids;
DROP TABLE IF EXISTS fixes;
DROP TABLE IF EXISTS procedures;
DROP TABLE IF EXISTS procedure_waypoints;
DROP TABLE IF EXISTS airways;

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE airports (
    icao TEXT PRIMARY KEY,
    name TEXT,
    lat  REAL,
    lon  REAL
);

CREATE TABLE runways (
    icao      TEXT,
    ident     TEXT,      -- '09L'
    length_ft INTEGER,
    heading   INTEGER,   -- магнитный курс
    lat       REAL,      -- координаты порога
    lon       REAL,
    PRIMARY KEY (icao, ident)
);

CREATE TABLE navaids (
    ident TEXT, name TEXT, type TEXT, lat REAL, lon REAL, freq TEXT
);

CREATE TABLE fixes (
    ident TEXT, lat REAL, lon REAL
);

CREATE TABLE procedures (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    icao    TEXT,
    kind    TEXT,        -- 'SID' | 'STAR' | 'APPROACH'
    name    TEXT,
    runways TEXT         -- сырой атрибут: 'All' / '09L' / 'ILS09L' и т.п.
);

CREATE TABLE procedure_waypoints (
    proc_id         INTEGER,
    seq             INTEGER,
    name            TEXT,
    wtype           TEXT,
    lat             REAL,
    lon             REAL,
    altitude        INTEGER,
    alt_restriction TEXT,
    speed           INTEGER
);

CREATE TABLE airways (
    airway TEXT,
    seq    INTEGER,
    ident  TEXT,
    lat    REAL,
    lon    REAL
);

CREATE INDEX idx_rwy_icao  ON runways(icao);
CREATE INDEX idx_proc_icao ON procedures(icao, kind);
CREATE INDEX idx_pwp_proc  ON procedure_waypoints(proc_id);
CREATE INDEX idx_navaid_id ON navaids(ident);
CREATE INDEX idx_fix_id    ON fixes(ident);
CREATE INDEX idx_awy_name  ON airways(airway, seq);
CREATE INDEX idx_awy_ident ON airways(ident);
"""


def _lines(zf: zipfile.ZipFile, name: str):
    """Строки текстового файла из архива без комментариев (';') и пустых."""
    try:
        raw = zf.read(name)
    except KeyError:
        return
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line and not line.startswith(";"):
            yield line


# --------------------------------------------------------------------------- #
#  Парсеры текстовых файлов
# --------------------------------------------------------------------------- #
def parse_airports(zf):
    rows = []
    pat = re.compile(r"^(\S+?)\s*(-?\d+\.\d+)\s*(-?\d+\.\d+)\s*$")
    for line in _lines(zf, "airports.dat"):
        m = pat.match(line)
        if m:
            icao, lat, lon = m.group(1), float(m.group(2)), float(m.group(3))
            rows.append((icao, icao, lat, lon))  # name заполним из wpnavapt
    return rows


def parse_runways(zf):
    """wpnavapt.txt — фиксированная ширина + координаты как знаковые float."""
    rows = []
    names = {}  # icao -> человекочитаемое имя аэропорта
    for line in _lines(zf, "wpnavapt.txt"):
        if len(line) < 39:
            continue
        name = line[0:24].strip()
        icao = line[24:28].strip()
        rwy = line[28:31].strip()
        try:
            length = int(line[31:36])
            heading = int(line[36:39])
        except ValueError:
            continue
        coords = FLOAT_RE.findall(line[39:])
        if len(coords) < 2:
            continue
        lat, lon = float(coords[0]), float(coords[1])
        rows.append((icao, rwy, length, heading, lat, lon))
        names.setdefault(icao, name)
    return rows, names


def parse_fixes(zf):
    rows = []
    for line in _lines(zf, "wpnavfix.txt"):
        if len(line) < 25:
            continue
        ident = line[0:24].strip()
        coords = FLOAT_RE.findall(line[24:])
        if len(coords) < 2:
            continue
        rows.append((ident, float(coords[0]), float(coords[1])))
    return rows


def parse_airways(zf):
    """wpnavrte.txt: 'AIRWAY SEQ IDENT LAT LON' — упорядоченные цепочки точек."""
    rows = []
    for line in _lines(zf, "wpnavrte.txt"):
        parts = line.split()
        if len(parts) < 5:
            continue
        awy, seq, ident, lat, lon = parts[0], parts[1], parts[2], parts[3], parts[4]
        try:
            rows.append((awy, int(seq), ident, float(lat), float(lon)))
        except ValueError:
            continue
    return rows


def parse_navaids(zf):
    rows = []
    for line in _lines(zf, "wpnavaid.txt"):
        if len(line) < 33:
            continue
        name = line[0:24].strip()
        ident = line[24:29].strip()
        typ = line[29:33].strip()
        tail = line[33:]
        coords = FLOAT_RE.findall(tail)
        if len(coords) < 2:
            continue
        lat, lon = float(coords[0]), float(coords[1])
        # частота — следующее число после координат (если есть)
        freq = coords[2] if len(coords) > 2 else ""
        rows.append((ident, name, typ, lat, lon, freq))
    return rows


# --------------------------------------------------------------------------- #
#  Парсер XML процедур
# --------------------------------------------------------------------------- #
def _txt(elem, tag, default=None):
    child = elem.find("{*}" + tag)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def _int(elem, tag, default=0):
    val = _txt(elem, tag)
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def parse_procedure_xml(data: bytes):
    """Возвращает список (proc_dict, [waypoint_dict, ...]) для одного аэропорта."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None, []
    airport = root.find("{*}Airport")
    if airport is None:
        return None, []
    icao = airport.get("ICAOcode")
    if not icao:
        return None, []

    procedures = []
    for kind, tag, wp_tag in (
        ("SID", "Sid", "Sid_Waypoint"),
        ("STAR", "Star", "Star_Waypoint"),
        ("APPROACH", "Approach", "App_Waypoint"),
    ):
        for proc in airport.findall("{*}" + tag):
            name = proc.get("Name", "")
            runways = proc.get("Runways", "")
            wps = []
            for seq, wp in enumerate(proc.findall("{*}" + wp_tag), start=1):
                wps.append(
                    {
                        "seq": seq,
                        "name": _txt(wp, "Name", ""),
                        "wtype": _txt(wp, "Type", ""),
                        "lat": float(_txt(wp, "Latitude", "0") or 0),
                        "lon": float(_txt(wp, "Longitude", "0") or 0),
                        "altitude": _int(wp, "Altitude"),
                        "alt_restriction": _txt(wp, "AltitudeRestriction", ""),
                        "speed": _int(wp, "Speed"),
                    }
                )
            procedures.append(
                ({"icao": icao, "kind": kind, "name": name, "runways": runways}, wps)
            )
    return icao, procedures


# --------------------------------------------------------------------------- #
#  Главный импорт
# --------------------------------------------------------------------------- #
def build(zip_path: Path, db_path: Path):
    if not zip_path.exists():
        raise FileNotFoundError(f"Архив навданных не найден: {zip_path}")

    print(f"Источник : {zip_path}")
    print(f"База     : {db_path}")

    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    with zipfile.ZipFile(zip_path) as zf:
        # --- cycle_info ---
        cycle_lines = list(_lines(zf, "cycle_info.txt"))
        cycle_text = "\n".join(cycle_lines)
        m = re.search(r"AIRAC cycle\s*:\s*(\S+)", cycle_text)
        cycle = m.group(1) if m else "?"
        m = re.search(r"Valid \(from/to\):\s*(.+)", cycle_text)
        valid = m.group(1).strip() if m else ""
        conn.executemany(
            "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
            [("airac_cycle", cycle), ("airac_valid", valid)],
        )

        # --- ВПП + имена аэропортов ---
        rwy_rows, names = parse_runways(zf)
        conn.executemany(
            "INSERT OR IGNORE INTO runways VALUES(?,?,?,?,?,?)", rwy_rows
        )
        print(f"ВПП       : {len(rwy_rows)}")

        # --- аэропорты (имя берём из wpnavapt, координаты из airports.dat) ---
        apt_rows = parse_airports(zf)
        apt_rows = [
            (icao, names.get(icao, name), lat, lon)
            for (icao, name, lat, lon) in apt_rows
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO airports VALUES(?,?,?,?)", apt_rows
        )

        # Дополняем аэропорты, которых нет в airports.dat, но у которых есть ВПП:
        # координата = среднее порогов полос, имя — из wpnavapt.
        have = {r[0] for r in apt_rows}
        by_icao: dict[str, list] = {}
        for icao, _rwy, _len, _hdg, lat, lon in rwy_rows:
            if icao not in have:
                by_icao.setdefault(icao, []).append((lat, lon))
        backfill = [
            (
                icao,
                names.get(icao, icao),
                sum(p[0] for p in pts) / len(pts),
                sum(p[1] for p in pts) / len(pts),
            )
            for icao, pts in by_icao.items()
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO airports VALUES(?,?,?,?)", backfill
        )
        print(f"Аэропорты : {len(apt_rows)} + {len(backfill)} из ВПП")

        # --- fixes / navaids ---
        fix_rows = parse_fixes(zf)
        conn.executemany("INSERT INTO fixes VALUES(?,?,?)", fix_rows)
        print(f"Fixes     : {len(fix_rows)}")

        nav_rows = parse_navaids(zf)
        conn.executemany("INSERT INTO navaids VALUES(?,?,?,?,?,?)", nav_rows)
        print(f"Navaids   : {len(nav_rows)}")

        # --- воздушные трассы ---
        awy_rows = parse_airways(zf)
        conn.executemany("INSERT INTO airways VALUES(?,?,?,?,?)", awy_rows)
        print(f"Трассы(точ): {len(awy_rows)}")

        # --- процедуры (XML по аэропортам) ---
        proc_count = wp_count = apt_with_proc = 0
        for info in zf.infolist():
            if not info.filename.lower().endswith(".xml"):
                continue
            icao, procedures = parse_procedure_xml(zf.read(info.filename))
            if not procedures:
                continue
            apt_with_proc += 1
            for proc, wps in procedures:
                cur = conn.execute(
                    "INSERT INTO procedures(icao,kind,name,runways) VALUES(?,?,?,?)",
                    (proc["icao"], proc["kind"], proc["name"], proc["runways"]),
                )
                pid = cur.lastrowid
                proc_count += 1
                conn.executemany(
                    "INSERT INTO procedure_waypoints "
                    "(proc_id,seq,name,wtype,lat,lon,altitude,alt_restriction,speed) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            pid,
                            w["seq"],
                            w["name"],
                            w["wtype"],
                            w["lat"],
                            w["lon"],
                            w["altitude"],
                            w["alt_restriction"],
                            w["speed"],
                        )
                        for w in wps
                    ],
                )
                wp_count += len(wps)

        print(f"Аэропорты с процедурами: {apt_with_proc}")
        print(f"Процедуры : {proc_count}")
        print(f"Точки проц: {wp_count}")

    conn.commit()
    conn.close()
    print(f"\nAIRAC {cycle} ({valid}) — импорт завершён.")


def main():
    zip_path = Path(sys.argv[1]) if len(sys.argv) > 1 else config.NAVDATA_ZIP
    build(zip_path, config.DB_PATH)


if __name__ == "__main__":
    main()
