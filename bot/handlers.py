"""Хендлеры Telegram-бота (aiogram 3.x)."""
from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from core import aircraft, planner, runway, weather
from navdata import db

router = Router()

ICAO_RE = re.compile(r"^[A-Za-z]{4}$")

HELP = (
    "✈️ <b>Планировщик полётов MSFS 2020 (IFR / коммерческий)</b>\n"
    "Навбаза: Navigraph AIRAC, погода: aviationweather.gov\n\n"
    "<b>Команды:</b>\n"
    "/plan — полный план: тип ВС, эшелон/время/топливо, активные ВПП,\n"
    "        SID/STAR/заход, маршрут по трассам (верхние) и запасной\n"
    "/metar &lt;ICAO&gt; — текущая погода + расшифровка кода\n"
    "/taf &lt;ICAO&gt; — прогноз TAF + расшифровка\n"
    "/rwy &lt;ICAO&gt; — активная ВПП по ветру\n"
    "/cancel — отменить ввод\n\n"
    "Пример: <code>/metar UUEE</code>"
)


class PlanFSM(StatesGroup):
    departure = State()
    destination = State()
    aircraft = State()


def _aircraft_keyboard() -> ReplyKeyboardMarkup:
    btns = [KeyboardButton(text=a.code) for a in aircraft.all_aircraft()]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=True)


def _aircraft_list_text() -> str:
    return "\n".join(f"• <b>{a.code}</b> — {a.name}" for a in aircraft.all_aircraft())


# --------------------------------------------------------------------------- #
#  Базовые команды
# --------------------------------------------------------------------------- #
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(HELP)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. /help — список команд.")


def _icao_arg(command: CommandObject) -> str | None:
    if not command.args:
        return None
    token = command.args.strip().split()[0].upper()
    return token if ICAO_RE.match(token) else None


@router.message(Command("metar"))
async def cmd_metar(message: Message, command: CommandObject):
    icao = _icao_arg(command)
    if not icao:
        await message.answer("Укажите ICAO: <code>/metar UUEE</code>")
        return
    m = await weather.fetch_metar(icao)
    if m is None:
        await message.answer(f"METAR для {icao} недоступен.")
        return
    await message.answer(weather.format_metar(m))


@router.message(Command("taf"))
async def cmd_taf(message: Message, command: CommandObject):
    icao = _icao_arg(command)
    if not icao:
        await message.answer("Укажите ICAO: <code>/taf EGLL</code>")
        return
    taf = await weather.fetch_taf(icao)
    if not taf:
        await message.answer(f"TAF для {icao} недоступен.")
        return
    await message.answer(weather.format_taf(taf, icao))


@router.message(Command("rwy"))
async def cmd_rwy(message: Message, command: CommandObject):
    icao = _icao_arg(command)
    if not icao:
        await message.answer("Укажите ICAO: <code>/rwy KLAX</code>")
        return
    ap = db.get_airport(icao)
    if ap is None:
        await message.answer(f"Аэропорт {icao} не найден в базе.")
        return
    m = await weather.fetch_metar(icao)
    ranked = runway.rank_runways(db.get_runways(icao), m)
    if not ranked:
        await message.answer(f"Нет данных о ВПП для {icao}.")
        return
    calm = m is None or m.wind_variable or m.wind_dir is None or m.wind_speed <= 3
    lines = [f"<b>{icao}</b> {ap.name}", weather.format_metar(m).splitlines()[2] if m else ""]
    lines.append("\n<b>ВПП по предпочтительности:</b>")
    for rw in ranked:
        mark = "✅ " if rw is ranked[0] else "   "
        lines.append(mark + runway.format_runway_choice(rw, calm))
    await message.answer("\n".join(l for l in lines if l))


# --------------------------------------------------------------------------- #
#  Сценарий /plan
# --------------------------------------------------------------------------- #
@router.message(Command("plan"))
async def cmd_plan(message: Message, state: FSMContext):
    await state.set_state(PlanFSM.departure)
    await message.answer("🛫 Введите ICAO аэропорта <b>вылета</b> (4 буквы):")


@router.message(PlanFSM.departure, F.text)
async def plan_departure(message: Message, state: FSMContext):
    icao = message.text.strip().upper()
    if not ICAO_RE.match(icao):
        await message.answer("Нужен код ICAO из 4 букв. Повторите или /cancel.")
        return
    if not db.airport_exists(icao):
        await message.answer(f"Аэропорт {icao} не найден. Повторите или /cancel.")
        return
    await state.update_data(dep=icao)
    await state.set_state(PlanFSM.destination)
    await message.answer(f"✅ Вылет: <b>{icao}</b>\n🛬 Теперь ICAO <b>назначения</b>:")


@router.message(PlanFSM.destination, F.text)
async def plan_destination(message: Message, state: FSMContext):
    dest = message.text.strip().upper()
    if not ICAO_RE.match(dest):
        await message.answer("Нужен код ICAO из 4 букв. Повторите или /cancel.")
        return
    if not db.airport_exists(dest):
        await message.answer(f"Аэропорт {dest} не найден. Повторите или /cancel.")
        return
    await state.update_data(dest=dest)
    await state.set_state(PlanFSM.aircraft)
    await message.answer(
        f"✅ Назначение: <b>{dest}</b>\n🛩 Выберите тип ВС:\n{_aircraft_list_text()}",
        reply_markup=_aircraft_keyboard(),
    )


@router.message(PlanFSM.aircraft, F.text)
async def plan_aircraft(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    if aircraft.get(code) is None:
        await message.answer("Выберите тип ВС кнопкой или введите его код. /cancel — отмена.")
        return
    data = await state.get_data()
    dep, dest = data["dep"], data["dest"]
    await state.clear()
    await message.answer("⏳ Строю план, запрашиваю погоду…", reply_markup=ReplyKeyboardRemove())
    try:
        card = await planner.build_plan(dep, dest, code)
    except Exception:
        logging.exception("build_plan failed: %s → %s (%s)", dep, dest, code)
        card = ("❌ Не удалось построить план — сбой сети или погодного сервиса. "
                "Попробуйте позже.")
    await message.answer(card)
