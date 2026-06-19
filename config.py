"""Конфигурация проекта: пути и параметры окружения."""
import os
from pathlib import Path

from dotenv import load_dotenv

# Корень проекта
BASE_DIR = Path(__file__).resolve().parent

# .env ищем рядом с проектом, а не относительно текущего каталога —
# иначе при автозапуске (CWD = system32) токен не подхватится.
load_dotenv(BASE_DIR / ".env")
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Путь к итоговой базе навданных
DB_PATH = DATA_DIR / "navdata.sqlite"

# Архив Navigraph native (источник навданных). Можно переопределить через .env
NAVDATA_ZIP = Path(
    os.getenv("NAVDATA_ZIP", str(BASE_DIR / "navdata_native.zip"))
)

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Хостинг / webhook (для запуска на Render и т.п.)
# Публичный базовый URL сервиса. На Render переменная RENDER_EXTERNAL_URL
# выставляется автоматически — тогда бот сам перейдёт в режим webhook.
# Локально переменной нет → бот работает в режиме polling, как раньше.
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE") or os.getenv("RENDER_EXTERNAL_URL", "")
# Секрет для проверки, что входящий запрос действительно от Telegram
# (заголовок X-Telegram-Bot-Api-Secret-Token).
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", BOT_TOKEN.split(":")[-1] or "flightplanner")
# Порт HTTP-сервера: Render передаёт его автоматически через переменную PORT.
PORT = int(os.getenv("PORT", "10000"))

# Погода
WEATHER_BASE_URL = "https://aviationweather.gov/api/data"
WEATHER_CACHE_TTL = 600  # секунд (10 минут)
