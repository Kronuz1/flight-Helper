"""Точка входа Telegram-бота «Планировщик полётов MSFS 2020».

Два режима запуска (выбираются автоматически):
  • polling  — локально, если публичный URL не задан (как и раньше);
  • webhook  — на хостинге (Render и т.п.), когда задан WEBHOOK_BASE
               или автоматически определён RENDER_EXTERNAL_URL. Тогда поднимается
               небольшой HTTP-сервер, а Telegram сам шлёт на него обновления.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import config
from bot.handlers import router

WEBHOOK_PATH = "/webhook"


def _setup_logging() -> None:
    # Пишем и в консоль, и в файл — при тихом запуске (pythonw) виден только лог.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.DATA_DIR / "bot.log", encoding="utf-8"),
        ],
    )


def _check_prereqs() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit(
            "Не задан BOT_TOKEN. Локально создайте файл .env, на хостинге —\n"
            "задайте переменную окружения BOT_TOKEN (токен от @BotFather)."
        )
    if not config.DB_PATH.exists():
        raise SystemExit(
            "Не найдена навбаза data/navdata.sqlite. Сначала выполните импорт:\n"
            "  python -m navdata.importer"
        )


def _make_bot_dp() -> tuple[Bot, Dispatcher]:
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    return bot, dp


async def run_polling() -> None:
    """Локальный режим: бот сам опрашивает Telegram."""
    bot, dp = _make_bot_dp()
    logging.info("Бот запущен (polling). Навбаза: %s", config.DB_PATH)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


def run_webhook() -> None:
    """Хостинг-режим: HTTP-сервер принимает обновления от Telegram."""
    bot, dp = _make_bot_dp()
    webhook_url = config.WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH

    async def on_startup(_: web.Application) -> None:
        await bot.set_webhook(
            webhook_url,
            secret_token=config.WEBHOOK_SECRET,
            drop_pending_updates=True,
        )
        logging.info("Webhook установлен: %s", webhook_url)

    async def on_cleanup(_: web.Application) -> None:
        await bot.delete_webhook()
        await bot.session.close()

    async def health(_: web.Request) -> web.Response:
        # Корневой маршрут для health-check'а хостинга.
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=config.WEBHOOK_SECRET,
    ).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    logging.info(
        "Бот запущен (webhook) на порту %d. Навбаза: %s", config.PORT, config.DB_PATH
    )
    web.run_app(app, host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    _setup_logging()
    _check_prereqs()
    try:
        if config.WEBHOOK_BASE:
            run_webhook()
        else:
            asyncio.run(run_polling())
    except SystemExit:
        raise
    except Exception:
        logging.exception("Бот завершился с ошибкой")
        raise
