"""Погода: загрузка и парсинг METAR/TAF с aviationweather.gov.

API бесплатный, без ключа и регистрации (NOAA/NWS Aviation Weather Center).
Глобальное покрытие. Лимит 100 запросов/мин — закрывается кэшем (TTL 10 мин).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import aiohttp

import config

# ---- простой in-memory кэш: ключ -> (timestamp, value) -------------------- #
_cache: dict[str, tuple[float, object]] = {}


def _cache_get(key: str):
    item = _cache.get(key)
    if item and (time.monotonic() - item[0]) < config.WEATHER_CACHE_TTL:
        return item[1]
    return None


def _cache_put(key: str, value) -> None:
    _cache[key] = (time.monotonic(), value)


@dataclass
class Metar:
    icao: str
    raw: str
    name: str = ""
    wind_dir: int | None = None  # None = штиль или переменный (см. wind_variable)
    wind_variable: bool = False
    wind_speed: int = 0  # узлы
    wind_gust: int | None = None
    visibility: str = ""
    qnh: int | None = None  # гПа
    temp: int | None = None
    dewp: int | None = None
    clouds: list[str] = field(default_factory=list)
    flight_category: str = ""
    report_time: str = ""


def _parse_metar(d: dict) -> Metar:
    wdir = d.get("wdir")
    wind_variable = False
    wind_dir: int | None = None
    if isinstance(wdir, str):
        if wdir.upper() == "VRB":
            wind_variable = True
        elif wdir.isdigit():
            wind_dir = int(wdir)
    elif isinstance(wdir, (int, float)):
        wind_dir = int(wdir)

    clouds = []
    for layer in d.get("clouds") or []:
        cover = layer.get("cover", "")
        base = layer.get("base")
        clouds.append(f"{cover} {base}фт" if base is not None else cover)

    return Metar(
        icao=d.get("icaoId", ""),
        raw=d.get("rawOb", ""),
        name=d.get("name", ""),
        wind_dir=wind_dir,
        wind_variable=wind_variable,
        wind_speed=int(d.get("wspd") or 0),
        wind_gust=int(d["wgst"]) if d.get("wgst") else None,
        visibility=str(d.get("visib", "")),
        qnh=int(round(d["altim"])) if d.get("altim") else None,
        temp=int(d["temp"]) if d.get("temp") is not None else None,
        dewp=int(d["dewp"]) if d.get("dewp") is not None else None,
        clouds=clouds,
        flight_category=d.get("fltCat", ""),
        report_time=d.get("reportTime", ""),
    )


async def _get_json(session: aiohttp.ClientSession, product: str, icao: str):
    url = f"{config.WEATHER_BASE_URL}/{product}"
    params = {"ids": icao, "format": "json"}
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
        r.raise_for_status()
        return await r.json()


async def fetch_metar(icao: str) -> Metar | None:
    icao = icao.strip().upper()
    key = f"metar:{icao}"
    cached = _cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    async with aiohttp.ClientSession() as s:
        data = await _get_json(s, "metar", icao)
    metar = _parse_metar(data[0]) if data else None
    _cache_put(key, metar)
    return metar


async def fetch_metars(icaos: list[str]) -> dict[str, Metar]:
    """Пакетный запрос METAR для нескольких аэропортов одним обращением к API."""
    icaos = [c.strip().upper() for c in icaos if c.strip()]
    if not icaos:
        return {}
    result: dict[str, Metar] = {}
    missing = []
    for c in icaos:
        cached = _cache_get(f"metar:{c}")
        if cached is not None:
            result[c] = cached  # type: ignore[assignment]
        else:
            missing.append(c)
    if missing:
        async with aiohttp.ClientSession() as s:
            data = await _get_json(s, "metar", ",".join(missing))
        for d in data or []:
            m = _parse_metar(d)
            result[m.icao] = m
            _cache_put(f"metar:{m.icao}", m)
    return result


async def fetch_taf(icao: str) -> str | None:
    """Возвращает сырой текст TAF (для информативного вывода)."""
    icao = icao.strip().upper()
    key = f"taf:{icao}"
    cached = _cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    async with aiohttp.ClientSession() as s:
        data = await _get_json(s, "taf", icao)
    taf = (data[0].get("rawTAF") or data[0].get("rawOb") or "") if data else None
    _cache_put(key, taf)
    return taf


# ---- расшифровка сырой METAR-строки ---------------------------------------- #
# Справочники для перевода токенов METAR на русский.
_WX_DESCRIPTOR = {
    "MI": "приземный",
    "PR": "частичный",
    "BC": "клочья",
    "DR": "поземок",
    "BL": "метель",
    "SH": "ливневый",
    "TS": "гроза",
    "FZ": "переохлаждённый",
}
_WX_PHENOMENON = {
    "DZ": "морось",
    "RA": "дождь",
    "SN": "снег",
    "SG": "снежные зёрна",
    "IC": "ледяные иглы",
    "PL": "ледяная крупа",
    "GR": "град",
    "GS": "мелкий град/снежная крупа",
    "UP": "неизвестные осадки",
    "BR": "дымка",
    "FG": "туман",
    "FU": "дым",
    "VA": "вулканический пепел",
    "DU": "пыль",
    "SA": "песок",
    "HZ": "мгла",
    "PY": "водяная пыль",
    "PO": "пыльные/песчаные вихри",
    "SQ": "шквал",
    "FC": "смерч",
    "SS": "песчаная буря",
    "DS": "пыльная буря",
}
_CLOUD_COVER = {
    "SKC": "ясно",
    "CLR": "ясно",
    "NSC": "без значимой облачности",
    "NCD": "облаков не обнаружено",
    "FEW": "незначительная (1–2 окта)",
    "SCT": "рассеянная (3–4 окта)",
    "BKN": "значительная (5–7 окта)",
    "OVC": "сплошная (8 окт)",
}
_CLOUD_TYPE = {"CB": "кучево-дождевые", "TCU": "мощные кучевые"}


def _decode_wind(tok: str) -> str | None:
    m = re.fullmatch(r"(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)", tok)
    if not m:
        return None
    direction, speed, gust, unit = m.groups()
    u = "м/с" if unit == "MPS" else "уз"
    if direction == "VRB":
        head = f"переменного направления, {int(speed)} {u}"
    elif direction == "000" and speed == "00":
        return "Ветер: штиль"
    else:
        head = f"{int(direction)}° (истинных), {int(speed)} {u}"
    if gust:
        head += f", порывы до {int(gust)} {u}"
    return f"Ветер: {head}"


def _decode_visibility(tok: str) -> str | None:
    if tok == "CAVOK":
        return "Видимость: CAVOK (≥10 км, без значимой облачности и явлений)"
    if tok == "9999":
        return "Видимость: 10 км и более"
    if re.fullmatch(r"\d{4}", tok):
        return f"Видимость: {int(tok)} м"
    m = re.fullmatch(r"(M)?(\d{1,2})(?:/(\d))?SM", tok)
    if m:
        less, whole, frac = m.groups()
        val = whole + (f" {whole}/{frac}".strip() if frac else "")
        prefix = "менее " if less else ""
        return f"Видимость: {prefix}{val} статутных миль"
    return None


# Осадки в творительном падеже — для конструкции «гроза с дождём».
_WX_WITH = {
    "RA": "дождём",
    "SN": "снегом",
    "GR": "градом",
    "GS": "снежной крупой",
    "DZ": "моросью",
    "PL": "ледяной крупой",
}


def _decode_weather(tok: str) -> str | None:
    body = tok
    intensity = ""
    if body.startswith("+"):
        intensity, body = "сильный ", body[1:]
    elif body.startswith("-"):
        intensity, body = "слабый ", body[1:]
    elif body.startswith("VC"):
        intensity, body = "вблизи аэродрома ", body[2:]

    descriptors: list[str] = []
    raw_descriptors: list[str] = []
    while len(body) >= 2 and body[:2] in _WX_DESCRIPTOR:
        raw_descriptors.append(body[:2])
        descriptors.append(_WX_DESCRIPTOR[body[:2]])
        body = body[2:]
    phenomena: list[str] = []
    raw_phenomena: list[str] = []
    while len(body) >= 2 and body[:2] in _WX_PHENOMENON:
        raw_phenomena.append(body[:2])
        phenomena.append(_WX_PHENOMENON[body[:2]])
        body = body[2:]
    if body or (not descriptors and not phenomena):
        return None

    # «гроза с дождём» вместо «гроза дождь»; для грозы — женский род прилагательного
    if raw_descriptors == ["TS"] and all(p in _WX_WITH for p in raw_phenomena):
        intensity = {"сильный ": "сильная ", "слабый ": "слабая "}.get(intensity, intensity)
        text = "гроза" + (" с " + ", ".join(_WX_WITH[p] for p in raw_phenomena)
                          if raw_phenomena else "")
    else:
        text = " ".join(descriptors + phenomena)
    return f"Явления: {intensity}{text}".strip()


def _decode_clouds(tok: str) -> str | None:
    if tok in ("SKC", "CLR", "NSC", "NCD"):
        return f"Облачность: {_CLOUD_COVER[tok]}"
    m = re.fullmatch(r"(FEW|SCT|BKN|OVC|VV)(\d{3})(CB|TCU)?", tok)
    if not m:
        return None
    cover, height, ctype = m.groups()
    feet = int(height) * 100
    if cover == "VV":
        return f"Облачность: вертикальная видимость {feet} фт (небо закрыто)"
    text = f"Облачность: {_CLOUD_COVER[cover]}, нижняя граница {feet} фт"
    if ctype:
        text += f", {_CLOUD_TYPE[ctype]}"
    return text


def _decode_temp(tok: str) -> str | None:
    m = re.fullmatch(r"(M?\d{2})/(M?\d{2})", tok)
    if not m:
        return None
    def num(s: str) -> int:
        return -int(s[1:]) if s.startswith("M") else int(s)
    t, td = num(m.group(1)), num(m.group(2))
    return f"Температура / точка росы: {t}°C / {td}°C"


def _decode_pressure(tok: str) -> str | None:
    m = re.fullmatch(r"Q(\d{4})", tok)
    if m:
        return f"Давление QNH: {int(m.group(1))} гПа"
    m = re.fullmatch(r"A(\d{4})", tok)
    if m:
        return f"Давление QNH: {int(m.group(1)) / 100:.2f} inHg"
    return None


def decode_metar_raw(raw: str) -> list[str]:
    """Построчно расшифровывает токены сырой METAR-строки на русский язык.

    Нераспознанные токены пропускаются — лучше показать меньше, чем выдать
    неверную расшифровку.
    """
    if not raw:
        return []
    tokens = raw.replace("=", "").split()
    lines: list[str] = []
    seen_rmk = False
    for tok in tokens:
        if re.fullmatch(r"[A-Z]{4}", tok) and not lines:
            continue  # код аэродрома — уже выводится в заголовке
        if re.fullmatch(r"\d{6}Z", tok):
            day, hh, mm = tok[:2], tok[2:4], tok[4:6]
            lines.append(f"Время наблюдения: {day}-е число, {hh}:{mm} UTC")
            continue
        if tok in ("AUTO", "COR", "NIL"):
            label = {"AUTO": "автоматическая станция (без участия наблюдателя)",
                     "COR": "исправленная сводка", "NIL": "данные отсутствуют"}[tok]
            lines.append(label.capitalize())
            continue
        if tok == "RMK":
            seen_rmk = True
            lines.append("RMK: служебные ремарки (далее)")
            continue
        if seen_rmk:
            continue  # ремарки не разбираем подробно
        for decoder in (_decode_wind, _decode_visibility, _decode_clouds,
                        _decode_temp, _decode_pressure, _decode_weather):
            res = decoder(tok)
            if res:
                lines.append(res)
                break
    return lines


# ---- расшифровка TAF ------------------------------------------------------- #
_TAF_VALID = re.compile(r"^(\d{2})(\d{2})/(\d{2})(\d{2})$")  # период DDHH/DDHH
_TAF_FM = re.compile(r"^FM(\d{2})(\d{2})(\d{2})$")           # FMDDHHmm
_TAF_PROB = re.compile(r"^PROB(\d{2})$")
_CHANGE_LABEL = {"TEMPO": "Временами", "BECMG": "Постепенно", "INTER": "Кратковременно"}


def _taf_period(m: re.Match) -> str:
    return f"{m.group(1)} {m.group(2)}:00–{m.group(3)} {m.group(4)}:00 UTC"


def _decode_taf_token(tok: str) -> str | None:
    for decoder in (_decode_wind, _decode_visibility, _decode_clouds, _decode_weather):
        res = decoder(tok)
        if res:
            return res
    return None


def decode_taf_raw(raw: str) -> list[str]:
    """Построчная расшифровка TAF: периоды действия и группы изменений на русском.

    Заголовок группы (основной прогноз / FM / TEMPO / BECMG / PROB) — отдельной
    строкой, метеотокены под ним с маркером «•» (через декодеры METAR).
    """
    if not raw:
        return []
    tokens = raw.replace("=", "").split()
    n = len(tokens)
    idx = 0
    while idx < n and tokens[idx] in ("TAF", "AMD", "COR"):
        idx += 1
    if idx < n and re.fullmatch(r"[A-Z]{4}", tokens[idx]):
        idx += 1  # код аэродрома — в заголовке
    lines: list[str] = []
    if idx < n and re.fullmatch(r"\d{6}Z", tokens[idx]):
        t = tokens[idx]
        lines.append(f"Выпуск: {t[:2]}-е число, {t[2:4]}:{t[4:6]} UTC")
        idx += 1
    if idx < n and (m := _TAF_VALID.match(tokens[idx])):
        lines.append(f"Период действия: {_taf_period(m)}")
        idx += 1
    lines.append("Основной прогноз:")

    while idx < n:
        tok = tokens[idx]
        if m := _TAF_FM.match(tok):
            lines.append(f"С {m.group(1)} {m.group(2)}:{m.group(3)} UTC:")
            idx += 1
            continue
        if tok in _CHANGE_LABEL:
            period = ""
            if idx + 1 < n and (pm := _TAF_VALID.match(tokens[idx + 1])):
                period = f" ({_taf_period(pm)})"
                idx += 1
            lines.append(f"{_CHANGE_LABEL[tok]}{period}:")
            idx += 1
            continue
        if mp := _TAF_PROB.match(tok):
            prob, extra = mp.group(1), ""
            nxt = tokens[idx + 1] if idx + 1 < n else ""
            if nxt in ("TEMPO", "INTER"):
                extra = " (" + _CHANGE_LABEL[nxt].lower() + ")"
                idx += 1
            period = ""
            if idx + 1 < n and (pm := _TAF_VALID.match(tokens[idx + 1])):
                period = f" {_taf_period(pm)}"
                idx += 1
            lines.append(f"Вероятность {prob}%{extra}{period}:")
            idx += 1
            continue
        res = _decode_taf_token(tok)
        if res:
            lines.append("• " + res)
        idx += 1
    return lines


def format_taf(raw: str, icao: str = "") -> str:
    if not raw:
        return "TAF недоступен."
    head = f"<b>TAF {icao}</b>".rstrip()
    lines = [head, f"<code>{raw}</code>"]
    decoded = decode_taf_raw(raw)
    if decoded:
        lines.append("\n📖 <b>Расшифровка:</b>")
        lines.extend(decoded)
    return "\n".join(lines)


# ---- форматирование для Telegram ------------------------------------------ #
def format_metar(m: Metar) -> str:
    if m is None:
        return "METAR недоступен."
    if m.wind_variable:
        wind = f"переменный {m.wind_speed} уз"
    elif m.wind_dir is None and m.wind_speed == 0:
        wind = "штиль"
    else:
        wind = f"{m.wind_dir:03d}° {m.wind_speed} уз"
    if m.wind_gust:
        wind += f", порывы {m.wind_gust} уз"

    lines = [
        f"<b>METAR {m.icao}</b> {('— ' + m.name) if m.name else ''}".rstrip(),
        f"<code>{m.raw}</code>",
        f"💨 Ветер: {wind}",
        f"👁 Видимость: {m.visibility}",
    ]
    if m.clouds:
        lines.append(f"☁️ Облачность: {', '.join(m.clouds)}")
    if m.qnh is not None:
        lines.append(f"🔵 QNH: {m.qnh} гПа")
    if m.temp is not None:
        lines.append(f"🌡 t/td: {m.temp}/{m.dewp}°C")
    if m.flight_category:
        lines.append(f"🚦 Категория: {m.flight_category}")
    if m.report_time:
        lines.append(f"🕓 {m.report_time}")

    decoded = decode_metar_raw(m.raw)
    if decoded:
        lines.append("\n📖 <b>Расшифровка кода:</b>")
        lines.extend(f"• {d}" for d in decoded)
    return "\n".join(lines)
