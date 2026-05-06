"""
Trucking Bot — всё в одном файле
python-telegram-bot v21 + SQLite
"""
import logging
import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime, time as dtime
from telegram import (
    Update, Bot,
    KeyboardButton, ReplyKeyboardMarkup,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler,
    ContextTypes, filters,
)

# ══════════════════════════════════════════════════════════════
# НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════
BOT_TOKEN    = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
OPERATOR_IDS = [int(x) for x in os.getenv("OPERATOR_IDS", "123456789").split(",")]
DB_PATH      = os.getenv("DB_PATH", "/app/data/trucking.db")
TEST_MODE    = os.getenv("TEST_MODE", "false").lower() == "true"

WEATHER_API_KEY   = os.getenv("WEATHER_API_KEY", "")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
WEATHER_UNITS   = "imperial"  # imperial = °F, metric = °C

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════
@contextmanager
def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()



# ══════════════════════════════════════════════════════════════
# ПОГОДА
# ══════════════════════════════════════════════════════════════
import asyncio
import urllib.request
import json as _json

WEATHER_EMOJI = {
    "Clear": "☀️", "Clouds": "☁️", "Rain": "🌧️",
    "Drizzle": "🌦️", "Thunderstorm": "⛈️", "Snow": "❄️",
    "Mist": "🌫️", "Fog": "🌫️", "Haze": "🌫️",
}

def _fetch_weather(city: str) -> dict:
    url = (
        f"https://api.openweathermap.org/data/2.5/weather"
        f"?q={urllib.request.quote(city)}&appid={WEATHER_API_KEY}"
        f"&units={WEATHER_UNITS}&lang=ru"
    )
    with urllib.request.urlopen(url, timeout=5) as r:
        return _json.loads(r.read())

def _fetch_forecast(city: str) -> dict:
    url = (
        f"https://api.openweathermap.org/data/2.5/forecast"
        f"?q={urllib.request.quote(city)}&appid={WEATHER_API_KEY}"
        f"&units={WEATHER_UNITS}&lang=ru&cnt=24"
    )
    with urllib.request.urlopen(url, timeout=5) as r:
        return _json.loads(r.read())

def format_weather_full(city: str, label: str = "") -> str:
    """Погода + прогноз на 3 дня для одного города."""
    if not WEATHER_API_KEY:
        return "⚠️ WEATHER_API_KEY не настроен."
    unit  = "°F" if WEATHER_UNITS == "imperial" else "°C"
    speed = "mph" if WEATHER_UNITS == "imperial" else "м/с"
    try:
        w     = _fetch_weather(city)
        main  = w["main"]
        wind  = w["wind"]
        cond  = w["weather"][0]
        emoji = WEATHER_EMOJI.get(cond["main"], "🌡️")
        city_name = w["name"]
        warn  = "\n⚠️ ОПАСНЫЕ УСЛОВИЯ!" if cond["main"] in SEVERE_CONDITIONS else ""

        header = f"{label} — {city_name}" if label else city_name
        lines = [
            f"{emoji} {header}",
            f"🌡 Сейчас: {main['temp']:.0f}{unit}, ощущается {main['feels_like']:.0f}{unit}",
            f"💧 Влажность: {main['humidity']}%",
            f"💨 Ветер: {wind['speed']:.1f} {speed}",
            f"🌥 {cond['description'].capitalize()}{warn}",
            "",
            "📅 Прогноз на 3 дня:",
        ]

        fc = _fetch_forecast(city)
        seen_days = set()
        for item in fc["list"]:
            dt  = datetime.fromtimestamp(item["dt"])
            day = dt.strftime("%a %d.%m")
            if day in seen_days:
                continue
            seen_days.add(day)
            if len(seen_days) > 3:
                break
            t    = item["main"]["temp"]
            t_min = item["main"]["temp_min"]
            t_max = item["main"]["temp_max"]
            desc  = item["weather"][0]["description"]
            em    = WEATHER_EMOJI.get(item["weather"][0]["main"], "🌡️")
            lines.append(f"{em} {day}: {t_max:.0f}/{t_min:.0f}{unit} — {desc}")

        return "\n".join(lines)

    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"❌ Город «{city}» не найден."
        return f"❌ Ошибка сервиса погоды: {e}"
    except Exception as e:
        return f"❌ Не удалось получить погоду для {city}: {e}"


def format_weather(city: str) -> str:
    if not WEATHER_API_KEY:
        return "⚠️ WEATHER_API_KEY не настроен. Добавьте ключ в переменные окружения."
    unit = "°F" if WEATHER_UNITS == "imperial" else "°C"
    speed = "mph" if WEATHER_UNITS == "imperial" else "м/с"
    try:
        w = _fetch_weather(city)
        main   = w["main"]
        wind   = w["wind"]
        cond   = w["weather"][0]
        emoji  = WEATHER_EMOJI.get(cond["main"], "🌡️")
        city_name = w["name"]

        lines = [
            f"{emoji} Погода в {city_name}",
            f"🌡 {main['temp']:.0f}{unit}, ощущается как {main['feels_like']:.0f}{unit}",
            f"💧 Влажность: {main['humidity']}%",
            f"💨 Ветер: {wind['speed']:.1f} {speed}",
            f"🌥 {cond['description'].capitalize()}",
            "",
            "📅 Прогноз на 3 дня:",
        ]

        fc = _fetch_forecast(city)
        seen_days = set()
        for item in fc["list"]:
            dt = datetime.fromtimestamp(item["dt"])
            day = dt.strftime("%a %d.%m")
            if day in seen_days:
                continue
            seen_days.add(day)
            if len(seen_days) > 3:
                break
            t    = item["main"]["temp"]
            desc = item["weather"][0]["description"]
            em   = WEATHER_EMOJI.get(item["weather"][0]["main"], "🌡️")
            lines.append(f"{em} {day}: {t:.0f}{unit} — {desc}")

        return "\n".join(lines)

    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"❌ Город «{city}» не найден. Попробуйте на английском: /weather New York"
        return f"❌ Ошибка погодного сервиса: {e}"
    except Exception as e:
        return f"❌ Не удалось получить погоду: {e}"


async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Укажите город: /weather New York\n"
            "Или: /weather Chicago"
        )
        return
    city = " ".join(context.args)
    msg  = await update.message.reply_text("⏳ Получаю погоду...")
    text = format_weather(city)
    await msg.edit_text(text)


# ══════════════════════════════════════════════════════════════
# ЖИВАЯ ГЕОЛОКАЦИЯ + ПОГОДА
# ══════════════════════════════════════════════════════════════
import math

# Хранилище активных трансляций: {user_id: {"lat", "lon", "last_weather", "chat_id"}}
live_locations: dict[int, dict] = {}

WEATHER_CHANGE_THRESHOLD = 50   # км — минимальный сдвиг для нового запроса
SEVERE_CONDITIONS = {           # опасные условия — уведомлять сразу
    "Thunderstorm", "Tornado", "Squall", "Snow", "Blizzard"
}

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Расстояние между двумя точками в км."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def get_weather_by_coords(lat: float, lon: float) -> dict | None:
    """Запрашивает погоду по координатам."""
    if not WEATHER_API_KEY:
        return None
    try:
        url = (
            f"https://api.openweathermap.org/data/2.5/weather"
            f"?lat={lat}&lon={lon}&appid={WEATHER_API_KEY}"
            f"&units={WEATHER_UNITS}&lang=ru"
        )
        with urllib.request.urlopen(url, timeout=5) as r:
            return _json.loads(r.read())
    except Exception as e:
        log.warning(f"Погода по координатам: {e}")
        return None


def format_weather_coords(data: dict) -> str:
    """Форматирует ответ погоды из данных API."""
    unit = "°F" if WEATHER_UNITS == "imperial" else "°C"
    speed = "mph" if WEATHER_UNITS == "imperial" else "м/с"
    city  = data.get("name", "Текущее местоположение")
    main  = data["main"]
    wind  = data["wind"]
    cond  = data["weather"][0]
    emoji = WEATHER_EMOJI.get(cond["main"], "🌡️")
    return (
        f"📍 {city}\n"
        f"{emoji} {cond['description'].capitalize()}\n"
        f"🌡 {main['temp']:.0f}{unit}, ощущается {main['feels_like']:.0f}{unit}\n"
        f"💧 Влажность: {main['humidity']}%\n"
        f"💨 Ветер: {wind['speed']:.1f} {speed}"
    )


def is_severe(data: dict) -> bool:
    """Проверяет опасные погодные условия."""
    return data["weather"][0]["main"] in SEVERE_CONDITIONS


async def handle_live_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает живую геолокацию от водителя."""
    msg = update.message or update.edited_message
    if not msg or not msg.location:
        return

    user_id = msg.from_user.id
    chat_id = msg.chat_id
    lat = msg.location.latitude
    lon = msg.location.longitude

    # Остановка трансляции
    if getattr(msg.location, 'live_period', None) is None and user_id in live_locations:
        del live_locations[user_id]
        await context.bot.send_message(
            chat_id=chat_id,
            text="📍 Трансляция геолокации остановлена. Отслеживание погоды завершено."
        )
        return

    prev = live_locations.get(user_id)

    # Первая точка — всегда отправляем погоду
    if not prev:
        live_locations[user_id] = {
            "lat": lat, "lon": lon,
            "last_weather": None, "chat_id": chat_id
        }
        data = get_weather_by_coords(lat, lon)
        if data:
            live_locations[user_id]["last_weather"] = data["weather"][0]["main"]
            text = (
                "🚛 Начало отслеживания маршрута\n\n"
                + format_weather_coords(data)
            )
            if is_severe(data):
                text += "\n\n⚠️ ОПАСНЫЕ УСЛОВИЯ! Рекомендуем остановиться."
            await context.bot.send_message(chat_id=chat_id, text=text)
        return

    # Считаем сдвиг
    dist = haversine_km(prev["lat"], prev["lon"], lat, lon)
    prev_condition = prev.get("last_weather")

    # Обновляем координаты
    live_locations[user_id]["lat"] = lat
    live_locations[user_id]["lon"] = lon

    # Запрашиваем погоду если сдвиг > порога
    if dist < WEATHER_CHANGE_THRESHOLD:
        return

    data = get_weather_by_coords(lat, lon)
    if not data:
        return

    new_condition = data["weather"][0]["main"]
    severe = is_severe(data)

    # Отправляем если: погода изменилась ИЛИ опасные условия
    if new_condition != prev_condition or severe:
        live_locations[user_id]["last_weather"] = new_condition
        text = f"📍 Обновление погоды — проехали {dist:.0f} км\n\n" + format_weather_coords(data)
        if severe:
            text += "\n\n⚠️ ОПАСНЫЕ УСЛОВИЯ! Рекомендуем остановиться."
        await context.bot.send_message(chat_id=chat_id, text=text)


# Хранилище маршрутов: {user_id: {"destination": str, "waypoints": [str]}}
route_data: dict[int, dict] = {}

ROUTE_CONV_SET_DEST = 900

async def cmd_liveweather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает маршрут перед стартом трансляции."""
    await update.message.reply_text(
        "🗺 Укажите маршрут в формате:\n\n"
        "<code>New York / Cleveland / Chicago</code>\n\n"
        "Первый город — промежуточные — последний город назначения.\n"
        "Можно без промежуточных: <code>New York / Chicago</code>",
        parse_mode="HTML"
    )
    return ROUTE_CONV_SET_DEST


async def st_route_dest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает маршрут, показывает погоду по всем точкам."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    cities = [c.strip() for c in text.split("/") if c.strip()]
    if len(cities) < 2:
        await update.message.reply_text(
            "Нужно минимум 2 города. Пример:\n<code>New York / Chicago</code>",
            parse_mode="HTML"
        )
        return ROUTE_CONV_SET_DEST

    route_data[user_id] = {"cities": cities, "chat_id": chat_id}

    # Отправляем погоду по всем точкам маршрута
    await update.message.reply_text(
        f"📋 Маршрут: {' → '.join(cities)}\n\nПолучаю погоду по всем точкам..."
    )

    for i, city in enumerate(cities):
        try:
            data = _fetch_weather(city)
            if i == 0:
                label = f"🚦 Старт — {city}"
            elif i == len(cities) - 1:
                label = f"🏁 Финиш — {city}"
            else:
                label = f"📍 Промежуток — {city}"

            unit  = "°F" if WEATHER_UNITS == "imperial" else "°C"
            speed = "mph" if WEATHER_UNITS == "imperial" else "м/с"
            cond  = data["weather"][0]
            main  = data["main"]
            wind  = data["wind"]
            emoji = WEATHER_EMOJI.get(cond["main"], "🌡️")
            severe_warn = "\n⚠️ ОПАСНЫЕ УСЛОВИЯ!" if cond["main"] in SEVERE_CONDITIONS else ""

            msg = (
                f"{label}\n"
                f"{emoji} {cond['description'].capitalize()}\n"
                f"🌡 {main['temp']:.0f}{unit}, ощущается {main['feels_like']:.0f}{unit}\n"
                f"💧 Влажность: {main['humidity']}%\n"
                f"💨 Ветер: {wind['speed']:.1f} {speed}"
                f"{severe_warn}"
            )
            await context.bot.send_message(chat_id=chat_id, text=msg)

        except Exception as e:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Не удалось получить погоду для {city}: {e}"
            )

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ Сводка по маршруту готова!\n\n"
            "Теперь включите живую геолокацию:\n"
            "📎 Скрепка → Location → Share Live Location\n"
            "Бот будет следить за погодой в пути автоматически."
        )
    )
    return ConversationHandler.END


async def conv_route_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════
# AI ПАРСЕР МАРШРУТА (Claude API)
# ══════════════════════════════════════════════════════════════
import urllib.error

def extract_cities_regex(text: str) -> list[str]:
    """Извлекает города из текста маршрута без AI — через regex."""
    import re

    # Ищем паттерны вида "City, ST" или "City, ST ZIP"
    pattern = re.compile(
        r"([A-Z][a-zA-Z\s\.\-]+),\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?",
        re.MULTILINE
    )
    matches = pattern.findall(text)
    cities = []
    seen = set()
    for city, state in matches:
        city = city.strip()
        # Убираем мусор — короткие слова, коды складов
        if len(city) < 3:
            continue
        # Убираем строки типа "ONT5", "EWR8" — коды складов Amazon
        if re.match(r"^[A-Z]{2,4}\d+$", city):
            continue
        key = f"{city}, {state}"
        if key not in seen:
            seen.add(key)
            cities.append(key)
    return cities


async def extract_cities_ai(text: str) -> list[str] | None:
    """Извлекает города из текста — сначала regex, без AI."""
    cities = extract_cities_regex(text)
    if len(cities) >= 2:
        log.info(f"Regex парсер нашёл города: {cities}")
        return cities
    log.warning(f"Regex не нашёл городов в тексте")
    return None


async def cmd_parsetrip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Парсит сообщение с маршрутом и отправляет погоду."""
    # Текст может быть в args или в reply
    if context.args:
        trip_text = " ".join(context.args)
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        trip_text = update.message.reply_to_message.text
    else:
        await update.message.reply_text(
            "Перешлите сообщение с маршрутом и ответьте на него командой /parsetrip\n\n"
            "Или отправьте: /parsetrip <текст маршрута>"
        )
        return

    msg = await update.message.reply_text("🔍 Анализирую маршрут...")

    # Пробуем AI парсер
    cities = await extract_cities_ai(trip_text)

    if not cities:
        await msg.edit_text(
            "❌ Не удалось извлечь маршрут.\n\n"
            "Добавьте GEMINI_API_KEY в переменные окружения на Bothost."
        )
        return

    await msg.edit_text(f"📋 Маршрут: {' → '.join(cities)}\n\nПолучаю погоду...")

    chat_id = update.effective_chat.id

    for i, city in enumerate(cities):
        label = "🚦 Старт" if i == 0 else "🏁 Финиш" if i == len(cities)-1 else "📍 Промежуток"
        text  = format_weather_full(city, label)
        await context.bot.send_message(chat_id=chat_id, text=text)

    await context.bot.send_message(
        chat_id=chat_id,
        text="✅ Сводка по маршруту готова!\n\nДля отслеживания в пути: /liveweather"
    )


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS drivers (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER UNIQUE NOT NULL,
            name       TEXT NOT NULL,
            phone      TEXT,
            active     INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS templates (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL,
            text       TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS schedules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL,
            text       TEXT,
            cron_expr  TEXT NOT NULL,
            target     TEXT NOT NULL,
            active     INTEGER DEFAULT 1,
            photo_id   TEXT,
            doc_id     TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        -- Миграция: добавляем колонки если их нет
        
        CREATE TABLE IF NOT EXISTS send_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id  INTEGER NOT NULL,
            text     TEXT,
            sent_at  TEXT DEFAULT (datetime('now')),
            source   TEXT
        );
        """)
        # Миграция: добавляем колонки если их нет
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(schedules)").fetchall()]
            if "photo_id" not in cols:
                conn.execute("ALTER TABLE schedules ADD COLUMN photo_id TEXT")
            if "doc_id" not in cols:
                conn.execute("ALTER TABLE schedules ADD COLUMN doc_id TEXT")
        except Exception:
            pass
        if conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
            conn.executemany("INSERT INTO templates (title, text) VALUES (?, ?)", [
                ("PTI напоминание",
                 "📋 Выполните Pre-Trip Inspection перед выездом.\n\n"
                 "Проверьте: документы, шины, тормоза, фары, прицеп.\n"
                 "Safe truck = Safe driver ✅"),
                ("Давление в колёсах",
                 "🛞 Проверьте давление в шинах:\n"
                 "• Передние (steer): 110–120 PSI\n"
                 "• Задние (drive): 95–105 PSI"),
                ("DOT Inspection Week",
                 "🚨 DOT Inspection Week!\n\n"
                 "Убедитесь, что все документы в порядке:\n"
                 "CDL, Medical Card, Registration, Insurance, ELD."),
                ("Техника безопасности",
                 "⚠️ Напоминание о безопасности:\n\n"
                 "• Пристегните ремень\n"
                 "• Соблюдайте скоростной режим\n"
                 "• Перерыв каждые 4 часа\n"
                 "• При усталости — остановитесь"),
            ])


# CRUD — водители
def add_driver(chat_id, name, phone=""):
    with get_conn() as conn:
        try:
            conn.execute("INSERT INTO drivers (chat_id, name, phone) VALUES (?,?,?)", (chat_id, name, phone))
            return True
        except sqlite3.IntegrityError:
            return False

def get_driver(chat_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM drivers WHERE chat_id=?", (chat_id,)).fetchone()

def get_all_drivers(active_only=True):
    with get_conn() as conn:
        q = "SELECT * FROM drivers" + (" WHERE active=1" if active_only else "") + " ORDER BY name"
        return conn.execute(q).fetchall()

def toggle_driver(chat_id, active):
    with get_conn() as conn:
        conn.execute("UPDATE drivers SET active=? WHERE chat_id=?", (1 if active else 0, chat_id))

def delete_driver(chat_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM drivers WHERE chat_id=?", (chat_id,))

# CRUD — шаблоны
def get_templates():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM templates ORDER BY title").fetchall()

def get_template(tid):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()

def add_template(title, text):
    with get_conn() as conn:
        return conn.execute("INSERT INTO templates (title,text) VALUES (?,?)", (title, text)).lastrowid

def delete_template(tid):
    with get_conn() as conn:
        conn.execute("DELETE FROM templates WHERE id=?", (tid,))

# CRUD — расписания
def get_schedules(active_only=False):
    with get_conn() as conn:
        q = "SELECT * FROM schedules" + (" WHERE active=1" if active_only else "") + " ORDER BY title"
        return conn.execute(q).fetchall()

def get_schedule(sid):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()

def add_schedule(title, text, cron_expr, target, photo_id=None, doc_id=None):
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO schedules (title,text,cron_expr,target,photo_id,doc_id) VALUES (?,?,?,?,?,?)",
            (title, text, cron_expr, target, photo_id, doc_id)
        ).lastrowid

def update_schedule(sid, **kw):
    fields = ", ".join(f"{k}=?" for k in kw)
    with get_conn() as conn:
        conn.execute(f"UPDATE schedules SET {fields} WHERE id=?", list(kw.values()) + [sid])

def delete_schedule(sid):
    with get_conn() as conn:
        conn.execute("DELETE FROM schedules WHERE id=?", (sid,))

def log_send(chat_id, text, source="manual"):
    with get_conn() as conn:
        conn.execute("INSERT INTO send_log (chat_id,text,source) VALUES (?,?,?)", (chat_id, text[:500], source))


# ══════════════════════════════════════════════════════════════
# ПЛАНИРОВЩИК
# ══════════════════════════════════════════════════════════════
def parse_cron(expr):
    parts = expr.strip().split("|")
    t = parts[0].strip()
    extra = parts[1].strip() if len(parts) > 1 else None
    if t.startswith("*/") and t.endswith("h"):
        return {"type": "interval", "seconds": int(t[2:-1]) * 3600}
    if t.startswith("*/") and t.endswith("m"):
        return {"type": "interval", "seconds": int(t[2:-1]) * 60}
    if ":" not in t:
        raise ValueError(f"Неверный формат: '{expr}'. Используйте 09:00, */4h или */10m")
    hh, mm = map(int, t.split(":"))
    r = {"type": "daily", "time": dtime(hour=hh, minute=mm)}
    if extra:
        wd = {"mon":0,"tue":1,"wed":2,"thu":3,"fri":4,"sat":5,"sun":6}
        if any(d in extra for d in wd):
            r["days"] = [wd[d] for d in extra.split(",") if d in wd]
        elif extra.isdigit():
            r["month_day"] = int(extra)
    return r


async def job_send_scheduled(context: ContextTypes.DEFAULT_TYPE):
    sid = context.job.data["sid"]
    s = get_schedule(sid)
    if not s or not s["active"]:
        return
    cron = parse_cron(s["cron_expr"])
    if "month_day" in cron and datetime.now().day > 7:
        return
    chat_ids = [d["chat_id"] for d in get_all_drivers()] if s["target"] == "all" \
        else [int(x) for x in s["target"].split(",") if x.strip()]
    photo_id = s["photo_id"] if "photo_id" in s.keys() else None
    doc_id   = s["doc_id"]   if "doc_id"   in s.keys() else None
    for cid in chat_ids:
        try:
            if photo_id:
                await context.bot.send_photo(chat_id=cid, photo=photo_id, caption=s["text"] or "")
            elif doc_id:
                await context.bot.send_document(chat_id=cid, document=doc_id, caption=s["text"] or "")
            else:
                await context.bot.send_message(chat_id=cid, text=s["text"])
            log_send(cid, s["text"] or "", "schedule")
        except Exception as e:
            log.warning(f"Расписание #{sid} → {cid}: {e}")


def register_schedule(app, s):
    unregister_schedule(app, s["id"])
    cron = parse_cron(s["cron_expr"])
    name = f"sched_{s['id']}"
    data = {"sid": s["id"]}
    if cron["type"] == "interval":
        app.job_queue.run_repeating(job_send_scheduled, interval=cron["seconds"],
                                    first=cron["seconds"], data=data, name=name)
    else:
        days = tuple(cron["days"]) if "days" in cron else tuple(range(7))
        app.job_queue.run_daily(job_send_scheduled, time=cron["time"], days=days, data=data, name=name)
    log.info(f"Расписание зарегистрировано: {name}")


def unregister_schedule(app, sid):
    for job in app.job_queue.get_jobs_by_name(f"sched_{sid}"):
        job.schedule_removal()


def register_all_schedules(app):
    for s in get_schedules(active_only=True):
        try:
            register_schedule(app, dict(s))
        except (ValueError, Exception) as e:
            log.warning(f"Расписание #{s['id']} пропущено ({s['cron_expr']}): {e}")
            delete_schedule(s['id'])


# ══════════════════════════════════════════════════════════════
# КЛАВИАТУРЫ И УТИЛИТЫ
# ══════════════════════════════════════════════════════════════
def is_op(uid): return uid in OPERATOR_IDS

def kb_main_op():
    return ReplyKeyboardMarkup([
        ["👥 Водители", "📋 Шаблоны"],
        ["🕐 Расписания", "📨 Рассылка"],
    ], resize_keyboard=True)

def kb_back(cb="back_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=cb)]])

def drivers_target_kb():
    rows = [[InlineKeyboardButton("📢 Всем водителям", callback_data="target_all")]]
    for d in get_all_drivers():
        rows.append([InlineKeyboardButton(f"👤 {d['name']}", callback_data=f"target_{d['chat_id']}")])
    return InlineKeyboardMarkup(rows)


# ══════════════════════════════════════════════════════════════
# СОСТОЯНИЯ ДИАЛОГОВ
# ══════════════════════════════════════════════════════════════
(
    ST_DRV_NAME, ST_DRV_CHAT,
    ST_TPL_TITLE, ST_TPL_TEXT,
    ST_SCH_TITLE, ST_SCH_TEXT, ST_SCH_CRON, ST_SCH_TARGET,
    ST_BC_TEXT, ST_BC_TARGET,
) = range(10)


# ══════════════════════════════════════════════════════════════
# ХЭНДЛЕРЫ
# ══════════════════════════════════════════════════════════════

# ── /start ───────────────────────────────────────────────────
async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает Telegram ID пользователя."""
    uid = update.effective_user.id
    name = update.effective_user.full_name
    is_operator = is_op(uid)
    await update.message.reply_text(
        f"👤 Ваш Telegram ID: <code>{uid}</code>\n"
        f"Имя: {name}\n"
        f"Оператор: {'✅ Да' if is_operator else '❌ Нет'}\n\n"
        f"Текущий OPERATOR_IDS: <code>{OPERATOR_IDS}</code>\n\n"
        + ("Кнопки должны работать!" if is_operator else
           f"⚠️ Добавьте <code>{uid}</code> в OPERATOR_IDS на Bothost и сделайте Restart."),
        parse_mode="HTML"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_op(uid):
        await update.message.reply_text("👨‍💼 Панель оператора:", reply_markup=kb_main_op())
    else:
        await update.message.reply_text("🚛 Trucking Bot активен.\nОжидайте уведомлений.")


# ── ВОДИТЕЛИ ─────────────────────────────────────────────────
async def sec_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_op(update.effective_user.id): return
    drivers = get_all_drivers(active_only=False)
    rows = []
    for d in drivers:
        icon = "✅" if d["active"] else "❌"
        rows.append([InlineKeyboardButton(f"{icon} {d['name']}", callback_data=f"drv_edit_{d['chat_id']}")])
    rows.append([InlineKeyboardButton("➕ Добавить водителя", callback_data="drv_add")])
    text = "👥 Водители:\n" + "\n".join(f"{'✅' if d['active'] else '❌'} {d['name']} ({d['chat_id']})" for d in drivers) if drivers else "👥 Пока нет водителей."
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))

async def cb_drv_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Введите имя водителя:")
    return ST_DRV_NAME

async def st_drv_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["drv_name"] = update.message.text.strip()
    await update.message.reply_text(
        "Введите chat_id группы водителя.\n\n"
        "Как узнать: добавьте @userinfobot в группу и напишите /start"
    )
    return ST_DRV_CHAT

async def st_drv_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Неверный формат. Введите числовой ID:")
        return ST_DRV_CHAT
    name = context.user_data.pop("drv_name", "Водитель")
    if add_driver(cid, name):
        await update.message.reply_text(f"✅ Водитель {name} добавлен.", reply_markup=kb_main_op())
    else:
        await update.message.reply_text(f"⚠️ Водитель с chat_id {cid} уже существует.", reply_markup=kb_main_op())
    return ConversationHandler.END

async def cb_drv_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = int(q.data.split("_")[-1])
    d = get_driver(cid)
    if not d:
        await q.message.reply_text("Не найден.")
        return
    lbl = "Деактивировать" if d["active"] else "Активировать"
    await q.message.reply_text(
        f"Водитель: {d['name']}\nЧат: {cid}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔄 {lbl}", callback_data=f"drv_toggle_{cid}")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"drv_del_{cid}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="nav_drivers")],
        ])
    )

async def cb_drv_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cid = int(q.data.split("_")[-1])
    d = get_driver(cid)
    if d:
        toggle_driver(cid, not d["active"])
        s = "активирован ✅" if not d["active"] else "деактивирован ❌"
        await q.message.reply_text(f"Водитель {d['name']} {s}.")

async def cb_drv_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cid = int(q.data.split("_")[-1])
    d = get_driver(cid)
    if d:
        delete_driver(cid)
        await q.message.reply_text(f"🗑 Водитель {d['name']} удалён.")


# ── ШАБЛОНЫ ──────────────────────────────────────────────────
async def sec_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_op(update.effective_user.id): return
    tpls = get_templates()
    rows = [[InlineKeyboardButton(t["title"], callback_data=f"tpl_view_{t['id']}")] for t in tpls]
    rows.append([InlineKeyboardButton("➕ Новый шаблон", callback_data="tpl_add")])
    await update.message.reply_text("📋 Шаблоны:", reply_markup=InlineKeyboardMarkup(rows))

async def cb_tpl_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    tid = int(q.data.split("_")[-1])
    t = get_template(tid)
    if not t: return
    await q.message.reply_text(
        f"📋 {t['title']}\n\n{t['text']}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📨 Отправить", callback_data=f"tpl_send_{tid}")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"tpl_del_{tid}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="nav_templates")],
        ])
    )

async def cb_tpl_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Введите название шаблона:")
    return ST_TPL_TITLE

async def st_tpl_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tpl_title"] = update.message.text.strip()
    await update.message.reply_text("Введите текст шаблона:")
    return ST_TPL_TEXT

async def st_tpl_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = context.user_data.pop("tpl_title", "")
    add_template(title, update.message.text.strip())
    await update.message.reply_text(f"✅ Шаблон «{title}» сохранён.", reply_markup=kb_main_op())
    return ConversationHandler.END

async def cb_tpl_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    tid = int(q.data.split("_")[-1])
    t = get_template(tid)
    if t:
        delete_template(tid)
        await q.message.reply_text(f"🗑 Шаблон «{t['title']}» удалён.")

async def cb_tpl_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    tid = int(q.data.split("_")[-1])
    t = get_template(tid)
    if not t: return ConversationHandler.END
    context.user_data["bc_text"] = t["text"]
    await q.message.reply_text(f"Шаблон: «{t['title']}»\n\nКому отправить?", reply_markup=drivers_target_kb())
    return ST_BC_TARGET


# ── РАСПИСАНИЯ ────────────────────────────────────────────────
async def sec_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_op(update.effective_user.id): return
    scheds = get_schedules()
    rows = []
    for s in scheds:
        icon = "✅" if s["active"] else "⏸"
        rows.append([InlineKeyboardButton(f"{icon} {s['title']} ({s['cron_expr']})", callback_data=f"sch_view_{s['id']}")])
    rows.append([InlineKeyboardButton("➕ Новое расписание", callback_data="sch_add")])
    await update.message.reply_text("🕐 Расписания:", reply_markup=InlineKeyboardMarkup(rows))

async def cb_sch_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = int(q.data.split("_")[-1])
    s = get_schedule(sid)
    if not s: return
    tgt = "Все водители" if s["target"] == "all" else s["target"]
    lbl = "⏸ Приостановить" if s["active"] else "▶️ Возобновить"
    await q.message.reply_text(
        f"🕐 {s['title']}\nРасписание: {s['cron_expr']}\nПолучатели: {tgt}\n\n{s['text']}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(lbl, callback_data=f"sch_toggle_{sid}")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"sch_del_{sid}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="nav_schedules")],
        ])
    )

async def cb_sch_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Введите название расписания:")
    return ST_SCH_TITLE

async def st_sch_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sch_title"] = update.message.text.strip()
    await update.message.reply_text("Введите текст уведомления:")
    return ST_SCH_TEXT

async def st_sch_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Поддержка фото/файла как контент уведомления
    if update.message.photo:
        context.user_data["sch_photo"] = update.message.photo[-1].file_id
        context.user_data["sch_text"] = update.message.caption or ""
    elif update.message.document:
        context.user_data["sch_doc"] = update.message.document.file_id
        context.user_data["sch_text"] = update.message.caption or ""
    else:
        context.user_data["sch_text"] = update.message.text.strip()
    await update.message.reply_text(
        "Введите расписание:\n\n"
        "<code>09:00</code> — каждый день\n"
        "<code>08:00|mon,wed,fri</code> — пн, ср, пт\n"
        "<code>09:00|1</code> — первая неделя месяца\n"
        "<code>*/4h</code> — каждые 4 часа\n"
        "<code>*/10m</code> — каждые 10 минут\n"
        "<code>*/4m</code> — каждые 4 минуты",
        parse_mode="HTML"
    )
    return ST_SCH_CRON

async def st_sch_cron(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sch_cron"] = update.message.text.strip()
    await update.message.reply_text("Кому отправлять?", reply_markup=drivers_target_kb())
    return ST_SCH_TARGET

async def st_sch_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    target = q.data.replace("target_", "")
    sid = add_schedule(
        context.user_data.pop("sch_title", ""),
        context.user_data.pop("sch_text", ""),
        context.user_data.pop("sch_cron", "09:00"),
        "all" if target == "all" else target,
        photo_id=context.user_data.pop("sch_photo", None),
        doc_id=context.user_data.pop("sch_doc", None),
    )
    register_schedule(context.application, dict(get_schedule(sid)))
    await q.message.reply_text("✅ Расписание создано.", reply_markup=kb_main_op())
    return ConversationHandler.END

async def cb_sch_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = int(q.data.split("_")[-1])
    s = get_schedule(sid)
    if not s: return
    new_active = 0 if s["active"] else 1
    update_schedule(sid, active=new_active)
    if new_active:
        register_schedule(context.application, dict(get_schedule(sid)))
        await q.message.reply_text(f"▶️ Расписание «{s['title']}» возобновлено.")
    else:
        unregister_schedule(context.application, sid)
        await q.message.reply_text(f"⏸ Расписание «{s['title']}» приостановлено.")

async def cb_sch_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = int(q.data.split("_")[-1])
    s = get_schedule(sid)
    if s:
        unregister_schedule(context.application, sid)
        delete_schedule(sid)
        await q.message.reply_text(f"🗑 Расписание «{s['title']}» удалено.")


# ── РАССЫЛКА ─────────────────────────────────────────────────
async def sec_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_op(update.effective_user.id): return ConversationHandler.END
    await update.message.reply_text("📨 Введите текст рассылки (или отправьте фото/файл с подписью):")
    return ST_BC_TEXT

async def st_bc_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        context.user_data["bc_photo"] = update.message.photo[-1].file_id
        context.user_data["bc_text"] = update.message.caption or ""
    elif update.message.document:
        context.user_data["bc_doc"] = update.message.document.file_id
        context.user_data["bc_text"] = update.message.caption or ""
    else:
        context.user_data["bc_text"] = update.message.text.strip()
    await update.message.reply_text("Кому отправить?", reply_markup=drivers_target_kb())
    return ST_BC_TARGET

async def st_bc_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    target = q.data.replace("target_", "")
    text  = context.user_data.pop("bc_text", "")
    photo = context.user_data.pop("bc_photo", None)
    doc   = context.user_data.pop("bc_doc", None)
    chat_ids = [d["chat_id"] for d in get_all_drivers()] if target == "all" else [int(target)]
    sent = 0
    for cid in chat_ids:
        try:
            if photo:   await context.bot.send_photo(chat_id=cid, photo=photo, caption=text)
            elif doc:   await context.bot.send_document(chat_id=cid, document=doc, caption=text)
            else:       await context.bot.send_message(chat_id=cid, text=text)
            log_send(cid, text)
            sent += 1
        except Exception as e:
            log.warning(f"Рассылка → {cid}: {e}")
    await q.message.reply_text(f"✅ Отправлено: {sent}/{len(chat_ids)}", reply_markup=kb_main_op())
    return ConversationHandler.END


# ── Навигация (callback) ──────────────────────────────────────
async def cb_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data
    if data == "back_main":
        await q.message.reply_text("Главное меню:", reply_markup=kb_main_op())
    elif data == "nav_drivers":
        drivers = get_all_drivers(active_only=False)
        rows = [[InlineKeyboardButton(("✅ " if d["active"] else "❌ ") + d["name"], callback_data=f"drv_edit_{d['chat_id']}")] for d in drivers]
        rows.append([InlineKeyboardButton("➕ Добавить", callback_data="drv_add")])
        await q.message.reply_text("👥 Водители:", reply_markup=InlineKeyboardMarkup(rows))
    elif data == "nav_templates":
        tpls = get_templates()
        rows = [[InlineKeyboardButton(t["title"], callback_data=f"tpl_view_{t['id']}")] for t in tpls]
        rows.append([InlineKeyboardButton("➕ Новый", callback_data="tpl_add")])
        await q.message.reply_text("📋 Шаблоны:", reply_markup=InlineKeyboardMarkup(rows))
    elif data == "nav_schedules":
        scheds = get_schedules()
        rows = [[InlineKeyboardButton(("✅ " if s["active"] else "⏸ ") + s["title"], callback_data=f"sch_view_{s['id']}")] for s in scheds]
        rows.append([InlineKeyboardButton("➕ Новое", callback_data="sch_add")])
        await q.message.reply_text("🕐 Расписания:", reply_markup=InlineKeyboardMarkup(rows))


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=kb_main_op())
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════
# СБОРКА И ЗАПУСК
# ══════════════════════════════════════════════════════════════
def build_route_conv():
    return ConversationHandler(
        entry_points=[CommandHandler("liveweather", cmd_liveweather)],
        states={
            ROUTE_CONV_SET_DEST: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_route_dest)],
        },
        fallbacks=[CommandHandler("cancel", conv_route_cancel)],
        per_user=True, per_chat=False,
    )


def build_conv():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_drv_add,  pattern="^drv_add$"),
            CallbackQueryHandler(cb_tpl_add,  pattern="^tpl_add$"),
            CallbackQueryHandler(cb_tpl_send, pattern=r"^tpl_send_\d+$"),
            CallbackQueryHandler(cb_sch_add,  pattern="^sch_add$"),
            MessageHandler(filters.Regex("^📨 Рассылка$"), sec_broadcast),
        ],
        states={
            ST_DRV_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, st_drv_name)],
            ST_DRV_CHAT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, st_drv_chat)],
            ST_TPL_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_tpl_title)],
            ST_TPL_TEXT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, st_tpl_text)],
            ST_SCH_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_sch_title)],
            ST_SCH_TEXT:  [MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, st_sch_text)],
            ST_SCH_CRON:  [MessageHandler(filters.TEXT & ~filters.COMMAND, st_sch_cron)],
            ST_SCH_TARGET:[CallbackQueryHandler(st_sch_target, pattern=r"^target_")],
            ST_BC_TEXT:   [MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, st_bc_text)],
            ST_BC_TARGET: [CallbackQueryHandler(st_bc_target, pattern=r"^target_")],
        },
        fallbacks=[CommandHandler("cancel", conv_cancel)],
        per_user=True, per_chat=False,
    )


async def auto_detect_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Автоматически определяет сообщение с маршрутом и предлагает погоду."""
    msg = update.message
    if not msg:
        return

    # Поддержка обычных и пересланных сообщений
    text = msg.text or msg.caption or ""
    if not text:
        return

    log.info(f"auto_detect_trip: получено сообщение от {msg.from_user.id} в чате {msg.chat_id}, тип чата: {msg.chat.type}")
    log.info(f"auto_detect_trip: текст[:100] = {repr(text[:100])}")

    # Нормализуем unicode (𝗧𝗿𝗶𝗽 → Trip, жирные/курсивные символы → обычные)
    import unicodedata
    normalized = unicodedata.normalize("NFKD", text)
    # Дополнительно убираем unicode bold/italic диапазоны вручную
    def strip_unicode_style(s):
        result = []
        for ch in s:
            cp = ord(ch)
            # Bold serif: 𝗔-𝘇 (U+1D400-U+1D7FF)
            if 0x1D400 <= cp <= 0x1D7FF:
                # Маппинг на обычные ASCII
                offsets = [
                    (0x1D400, 0x1D419, 65),   # Bold A-Z
                    (0x1D41A, 0x1D433, 97),   # Bold a-z
                    (0x1D434, 0x1D44D, 65),   # Italic A-Z
                    (0x1D44E, 0x1D467, 97),   # Italic a-z
                    (0x1D468, 0x1D481, 65),   # Bold Italic A-Z
                    (0x1D482, 0x1D49B, 97),   # Bold Italic a-z
                    (0x1D49C, 0x1D4B5, 65),   # Script A-Z
                    (0x1D5D4, 0x1D5ED, 65),   # Bold Sans A-Z
                    (0x1D5EE, 0x1D607, 97),   # Bold Sans a-z
                    (0x1D608, 0x1D621, 65),   # Italic Sans A-Z
                    (0x1D622, 0x1D63B, 97),   # Italic Sans a-z
                    (0x1D63C, 0x1D655, 65),   # Bold Italic Sans A-Z
                    (0x1D656, 0x1D66F, 97),   # Bold Italic Sans a-z
                ]
                converted = False
                for start, end, base in offsets:
                    if start <= cp <= end:
                        result.append(chr(base + cp - start))
                        converted = True
                        break
                if not converted:
                    result.append(ch)
            else:
                result.append(ch)
        return "".join(result)

    clean_text = strip_unicode_style(normalized)

    keywords = [
        "Trip ID", "trip id", "TRIP ID",
        "Loaded -", "Loaded-",
        "Per mile", "per mile",
        "Duration", "duration",
        "Preloaded", "preloaded",
        "Drop", "Pickup",
    ]
    matched = [kw for kw in keywords if kw.lower() in clean_text.lower()]
    log.info(f"auto_detect_trip: совпавшие ключевые слова: {matched}")
    if not matched:
        return

    # Сохраняем оригинальный текст для AI парсера
    text = clean_text
    await update.message.reply_text(
        "🚛 Вижу сообщение с маршрутом!\n"
        "Отправить погоду по всем точкам маршрута?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, показать погоду", callback_data=f"autotrip_{update.message.message_id}"),
            InlineKeyboardButton("❌ Нет", callback_data="autotrip_cancel"),
        ]])
    )
    # Сохраняем текст для последующей обработки
    context.bot_data[f"trip_msg_{update.message.message_id}"] = {
        "text": clean_text,
        "chat_id": update.effective_chat.id,
    }


async def cb_autotrip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает подтверждение автопарсинга маршрута."""
    q = update.callback_query
    await q.answer()

    if q.data == "autotrip_cancel":
        await q.message.delete()
        return

    msg_id = q.data.replace("autotrip_", "")
    saved  = context.bot_data.get(f"trip_msg_{msg_id}")
    if not saved:
        await q.message.edit_text("❌ Сообщение не найдено. Попробуйте /parsetrip")
        return

    await q.message.edit_text("🔍 Анализирую маршрут...")
    cities = await extract_cities_ai(saved["text"])

    if not cities:
        await q.message.edit_text(
            "❌ Не удалось извлечь маршрут.\n"
            "Убедитесь что добавлен GEMINI_API_KEY на Bothost."
        )
        return

    await q.message.edit_text(f"📋 Маршрут: {' → '.join(cities)}\n\nПолучаю погоду...")

    chat_id = saved["chat_id"]

    for i, city in enumerate(cities):
        label = "🚦 Старт" if i == 0 else "🏁 Финиш" if i == len(cities)-1 else "📍 Промежуток"
        text  = format_weather_full(city, label)
        await context.bot.send_message(chat_id=chat_id, text=text)

    await context.bot.send_message(
        chat_id=chat_id,
        text="✅ Готово! Для отслеживания в пути используйте /liveweather"
    )


def main():
    init_db()
    log.info("БД инициализирована.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("parsetrip", cmd_parsetrip))

    # ── Сначала ConversationHandler-ы ────────────────────────
    app.add_handler(build_route_conv())
    app.add_handler(build_conv())

    # ── Кнопки меню оператора (приоритет выше auto_detect) ───
    app.add_handler(MessageHandler(filters.Regex("^👥 Водители$"),   sec_drivers))
    app.add_handler(MessageHandler(filters.Regex("^📋 Шаблоны$"),    sec_templates))
    app.add_handler(MessageHandler(filters.Regex("^🕐 Расписания$"), sec_schedules))

    # ── Inline callbacks ──────────────────────────────────────
    app.add_handler(CallbackQueryHandler(cb_autotrip, pattern=r"^autotrip_"))

    # ── Геолокация ────────────────────────────────────────────
    app.add_handler(MessageHandler(filters.LOCATION, handle_live_location))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.LOCATION, handle_live_location))

    # ── Автодетект маршрута — ПОСЛЕДНИМ чтобы не перехватывать кнопки ──
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.FORWARDED) & ~filters.COMMAND,
        auto_detect_trip
    ))

    app.add_handler(CallbackQueryHandler(cb_drv_edit,   pattern=r"^drv_edit_-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_drv_toggle, pattern=r"^drv_toggle_-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_drv_del,    pattern=r"^drv_del_-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_tpl_view,   pattern=r"^tpl_view_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_tpl_del,    pattern=r"^tpl_del_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_sch_view,   pattern=r"^sch_view_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_sch_toggle, pattern=r"^sch_toggle_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_sch_del,    pattern=r"^sch_del_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_nav,        pattern=r"^(back_main|nav_drivers|nav_templates|nav_schedules)$"))

    async def on_start(app):
        register_all_schedules(app)
        log.info("Расписания загружены.")
    app.post_init = on_start

    log.info(f"Бот запущен. TEST_MODE={TEST_MODE}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
