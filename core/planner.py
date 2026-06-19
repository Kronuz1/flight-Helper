"""Сборка полного плана полёта в текстовую карточку для Telegram."""
from __future__ import annotations

import math
from dataclasses import dataclass

from core import aircraft, alternate, procedures, router, runway, weather
from core.runway import RunwayWind
from navdata import db
from navdata.db import Airport, Procedure


def _calm(m: weather.Metar | None) -> bool:
    return m is None or m.wind_variable or m.wind_dir is None or m.wind_speed <= 3


def great_circle_km(a: Airport, b: Airport) -> int:
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = math.radians(b.lat - a.lat)
    dl = math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(round(2 * r * math.asin(math.sqrt(h))))


def _proc_display(name: str) -> str:
    """Имя процедуры для маршрута без суффикса ВПП (EMGA3H.24L → EMGA3H)."""
    return name.split(".")[0]


@dataclass
class EndPlan:
    airport: Airport
    metar: weather.Metar | None
    rw: RunwayWind | None
    calm: bool


async def _analyze(icao: str) -> EndPlan:
    ap = db.get_airport(icao)
    metar = await weather.fetch_metar(icao)
    rw = runway.active_runway(db.get_runways(icao), metar)
    return EndPlan(ap, metar, rw, _calm(metar))


def _wind_line(m: weather.Metar | None) -> str:
    if m is None:
        return "METAR недоступен"
    if m.wind_variable:
        w = f"переменный {m.wind_speed} уз"
    elif m.wind_dir is None and m.wind_speed == 0:
        w = "штиль"
    else:
        w = f"{m.wind_dir:03d}°/{m.wind_speed} уз"
        if m.wind_gust:
            w += f" (порывы {m.wind_gust})"
    extra = f", QNH {m.qnh}" if m.qnh else ""
    cat = f", {m.flight_category}" if m.flight_category else ""
    return f"💨 {w}{extra}{cat}"


def _build_route(dep: Airport, sid: Procedure | None,
                 dest: Airport, star: Procedure | None) -> tuple[str, float]:
    """Строка маршрута в формате ИКАО и его геометрическая длина (морские мили)."""
    sid_exit = procedures._last_fix(sid) if sid else None
    star_entry = procedures._first_fix(star) if star else None
    gc = router.great_circle_nm

    tokens: list[str] = [dep.icao]
    if sid:
        tokens.append(_proc_display(sid.name))

    if sid_exit and star_entry:
        enroute, enroute_nm = router.route_with_distance(
            router.Fix(sid_exit.name, sid_exit.lat, sid_exit.lon),
            router.Fix(star_entry.name, star_entry.lat, star_entry.lon),
        )
        tokens += enroute
        total_nm = (gc(dep.lat, dep.lon, sid_exit.lat, sid_exit.lon)
                    + enroute_nm
                    + gc(star_entry.lat, star_entry.lon, dest.lat, dest.lon))
    elif sid_exit:
        tokens.append(sid_exit.name)
        total_nm = (gc(dep.lat, dep.lon, sid_exit.lat, sid_exit.lon)
                    + gc(sid_exit.lat, sid_exit.lon, dest.lat, dest.lon))
    elif star_entry:
        tokens += ["DCT", star_entry.name]
        total_nm = (gc(dep.lat, dep.lon, star_entry.lat, star_entry.lon)
                    + gc(star_entry.lat, star_entry.lon, dest.lat, dest.lon))
    else:
        total_nm = gc(dep.lat, dep.lon, dest.lat, dest.lon)

    if star:
        tokens.append(_proc_display(star.name))
    tokens.append(dest.icao)
    return " ".join(tokens), total_nm


async def build_plan(dep_icao: str, dest_icao: str, ac_code: str = "A320") -> str:
    dep_icao, dest_icao = dep_icao.upper(), dest_icao.upper()
    dep = db.get_airport(dep_icao)
    dest = db.get_airport(dest_icao)
    if dep is None:
        return f"❌ Аэропорт вылета <b>{dep_icao}</b> не найден в базе."
    if dest is None:
        return f"❌ Аэропорт назначения <b>{dest_icao}</b> не найден в базе."
    ac = aircraft.get(ac_code)
    if ac is None:
        return f"❌ Неизвестный тип ВС <b>{ac_code}</b>."

    d = await _analyze(dep_icao)
    a = await _analyze(dest_icao)
    info = db.airac_info()

    # Посадочную ВПП выбираем среди полос с опубликованным заходом:
    # оптимальная по ветру может не иметь захода (напр. URSS 24 — горы/море).
    land_rw = a.rw
    land_note = ""
    if a.rw:
        ranked = runway.rank_runways(db.get_runways(dest_icao), a.metar)
        appr_rwys = procedures.runways_with_approach(dest_icao)
        if appr_rwys:
            for rw in ranked:
                if any(procedures.runway_matches(ar, rw.runway.ident) for ar in appr_rwys):
                    if rw.runway.ident != a.rw.runway.ident:
                        land_note = (
                            f" (ВПП {a.rw.runway.ident} выгоднее по ветру, "
                            f"но без опубликованного захода)"
                        )
                    land_rw = rw
                    break

    # выбор процедур
    land_ident = land_rw.runway.ident if land_rw else ""
    sid_pick = procedures.select_sid(dep, d.rw.runway.ident, dest)[:1] if d.rw else []
    star_pick = procedures.select_star(dest, land_ident, dep)[:1] if land_rw else []
    app_pick = procedures.select_approach(dest, land_ident)[:1] if land_rw else []
    sid = sid_pick[0].procedure if sid_pick else None
    star = star_pick[0].procedure if star_pick else None

    # маршрут, реальная дистанция и запасной (нужен для расчёта топлива)
    route, route_nm = _build_route(dep, sid, dest, star)
    bad = alternate.is_bad_weather(a.metar)
    altn = await alternate.find_alternate(dest)
    altn_nm = altn.distance_km / 1.852 if altn else 0.0

    # эшелон / время / топливо
    track = procedures.bearing(dep.lat, dep.lon, dest.lat, dest.lon)
    fl = aircraft.cruise_fl_semicircular(track, ac.cruise_fl)
    ete = aircraft.format_hm(aircraft.ete_hours(route_nm, ac))
    fuel = aircraft.fuel_estimate(route_nm, altn_nm, ac)
    route_km = int(round(route_nm * 1.852))

    # предупреждения по длине ВПП под выбранное ВС
    warns: list[str] = []
    if d.rw and d.rw.runway.length_ft < ac.min_runway_ft:
        warns.append(f"⚠️ ВПП вылета {d.rw.runway.ident}: {d.rw.runway.length_ft} фт "
                     f"< потребных {ac.min_runway_ft} фт для {ac.code}")
    if land_rw and land_rw.runway.length_ft < ac.min_runway_ft:
        warns.append(f"⚠️ ВПП посадки {land_rw.runway.ident}: {land_rw.runway.length_ft} фт "
                     f"< потребных {ac.min_runway_ft} фт для {ac.code}")

    lines: list[str] = []
    lines.append(f"🛫 <b>ПЛАН ПОЛЁТА</b>  {dep_icao} → {dest_icao}")
    lines.append(f"<i>AIRAC {info.get('airac_cycle','?')} · {info.get('airac_valid','')}</i>")
    lines.append("")

    # ── ВС / ЭШЕЛОН / ТОПЛИВО ──
    lines.append("━━━ <b>ВС · ЭШЕЛОН · ТОПЛИВО</b> ━━━")
    lines.append(f"🛩 Тип: <b>{ac.code}</b> — {ac.name}")
    lines.append(f"📊 Эшелон: <b>FL{fl}</b> · крейсер {ac.cruise_tas} уз")
    lines.append(f"📏 Маршрут: ~{route_km} км ({int(round(route_nm))} nm)")
    lines.append(f"⏱ В пути (без ветра): ~{ete}")
    lines.append(f"⛽ Block fuel: <b>{fuel['block']}</b> кг")
    lines.append(f"   trip {fuel['trip']} · запасной {fuel['alternate']} · "
                 f"резерв {fuel['reserve']} · руление {fuel['taxi']} · непредв. {fuel['contingency']}")
    lines.extend(warns)
    lines.append("")

    # ── ВЫЛЕТ ──
    lines.append(f"━━━ <b>ВЫЛЕТ: {dep_icao}</b> {dep.name} ━━━")
    lines.append(_wind_line(d.metar))
    if d.rw:
        lines.append("🛬 " + runway.format_runway_choice(d.rw, d.calm))
        if sid_pick:
            lines.append(f"📐 SID: <b>{sid.name}</b> — {sid_pick[0].reason}")
        else:
            lines.append("📐 SID: подходящих по активной ВПП не найдено")
    else:
        lines.append("Нет данных о ВПП")
    lines.append("")

    # ── МАРШРУТ ──
    lines.append("━━━ <b>МАРШРУТ</b> ━━━")
    lines.append(f"<code>{route}</code>")
    lines.append("")

    # ── ПОСАДКА ──
    lines.append(f"━━━ <b>ПОСАДКА: {dest_icao}</b> {dest.name} ━━━")
    lines.append(_wind_line(a.metar))
    if land_rw:
        lines.append("🛬 " + runway.format_runway_choice(land_rw, a.calm) + land_note)
        if star_pick:
            lines.append(f"📐 STAR: <b>{star.name}</b> — {star_pick[0].reason}")
        else:
            lines.append("📐 STAR: подходящих не найдено")
        if app_pick:
            apps = procedures.select_approach(dest, land_ident)
            lines.append(f"🎯 Заход: <b>{app_pick[0].procedure.name}</b>")
            alt_apps = ", ".join(p.procedure.name for p in apps[1:4])
            if alt_apps:
                lines.append(f"   альт.: {alt_apps}")
        else:
            lines.append("🎯 Заход: не найден")
    else:
        lines.append("Нет данных о ВПП")

    # ── ЗАПАСНОЙ АЭРОДРОМ ──
    lines.append("")
    if bad:
        lines.append("⚠️ <b>В пункте назначения непогода — запасной:</b>")
    else:
        lines.append("🅰️ <b>Запасной аэродром:</b>")
    if altn:
        cat = f" [{altn.metar.flight_category}]" if altn.metar and altn.metar.flight_category else ""
        lines.append(f"{altn.airport.icao} {altn.airport.name} (~{altn.distance_km} км){cat}")
        if altn.metar:
            lines.append(f"   {_wind_line(altn.metar)}")
    else:
        lines.append("подходящий не найден в радиусе поиска")

    return "\n".join(lines)
