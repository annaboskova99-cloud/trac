"""
Trucking Bot — всё в одном файле
python-telegram-bot v21 + SQLite
"""
import asyncio
import logging
import math
import os
import re
import sqlite3
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import json as _json
from contextlib import contextmanager
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from telegram import (
    Update,
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
BOT_TOKEN         = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
OPERATOR_IDS      = [int(x) for x in os.getenv("OPERATOR_IDS", "123456789").split(",")]
# Группы где разрешена панель оператора (все участники могут управлять ботом)
OPERATOR_CHAT_IDS = [int(x) for x in os.getenv("OPERATOR_CHAT_IDS", "-1003961422554").split(",") if x.strip()]
DB_PATH           = os.getenv("DB_PATH", "/app/data/trucking.db")
TEST_MODE         = os.getenv("TEST_MODE", "false").lower() == "true"
WEATHER_API_KEY   = os.getenv("WEATHER_API_KEY", "")
GOOGLE_MAPS_KEY   = os.getenv("GOOGLE_MAPS_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
WEATHER_UNITS        = "imperial"  # imperial=°F, metric=°C
TRUCK_TIMEZONE       = os.getenv("TRUCK_TIMEZONE", "America/New_York")  # US timezone for alarms/notifications  # imperial=°F, metric=°C
DISPATCHER_GROUP_ID  = int(os.getenv("DISPATCHER_GROUP_ID", "0"))  # ID группы диспетчеров
ARRIVED_TIMEOUT_SEC  = int(os.getenv("ARRIVED_TIMEOUT_SEC", "300"))  # 5 минут
DISPATCH_GROUP_ID = int(os.getenv("DISPATCH_GROUP_ID", "0"))  # ID группы диспетчеров
ARRIVED_TIMEOUT   = int(os.getenv("ARRIVED_TIMEOUT", "300"))  # секунд до эскалации (300 = 5 мин)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

def now_local() -> datetime:
    """Current time in truck timezone."""
    try:
        return datetime.now(ZoneInfo(TRUCK_TIMEZONE))
    except Exception:
        return datetime.now()

def fmt_time(dt: datetime = None) -> str:
    """Format time in truck timezone: HH:MM ET."""
    if dt is None:
        dt = now_local()
    tz_abbr = dt.strftime("%Z") or "ET"
    return dt.strftime(f"%I:%M %p {tz_abbr}")

def fmt_datetime(dt: datetime = None) -> str:
    """Format date+time in truck timezone."""
    if dt is None:
        dt = now_local()
    tz_abbr = dt.strftime("%Z") or "ET"
    return dt.strftime(f"%m/%d/%Y %I:%M %p {tz_abbr}")

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


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS drivers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER UNIQUE NOT NULL,
            name TEXT NOT NULL,
            phone TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            text TEXT,
            cron_expr TEXT NOT NULL,
            target TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            photo_id TEXT,
            doc_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS send_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT,
            sent_at TEXT DEFAULT (datetime('now')),
            source TEXT
        );
        CREATE TABLE IF NOT EXISTS dispatcher_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER UNIQUE NOT NULL,
            title TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """)
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
                ("PTI Reminder",
                 "📋 Complete Pre-Trip Inspection before departure.\n\nCheck: documents, tires, brakes, lights, trailer.\nSafe truck = Safe driver ✅"),
                ("Tire Pressure Check",
                 "🛞 Check tire pressure:\n• Steer (front): 110–120 PSI\n• Drive (rear): 95–105 PSI"),
                ("DOT Inspection Week",
                 "🚨 DOT Inspection Week!\n\nMake sure all documents are in order:\nCDL, Medical Card, Registration, Insurance, ELD."),
                ("Техника безопасности",
                 "⚠️ Напоминание о безопасности:\n\n• Пристегните ремень\n• Соблюдайте скоростной режим\n• Перерыв every 4 hours\n• При усталости — остановитесь"),
            ])


# ── CRUD водители ─────────────────────────────────────────────
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

# ── CRUD группы диспетчеров ──────────────────────────────────
def add_dispatcher_group(chat_id: int, title: str) -> bool:
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO dispatcher_groups (chat_id, title) VALUES (?,?)",
                (chat_id, title)
            )
            return True
        except sqlite3.IntegrityError:
            # Обновляем title если уже есть
            conn.execute(
                "UPDATE dispatcher_groups SET title=?, active=1 WHERE chat_id=?",
                (title, chat_id)
            )
            return True

def get_dispatcher_groups(active_only=True) -> list:
    with get_conn() as conn:
        q = "SELECT * FROM dispatcher_groups"
        if active_only:
            q += " WHERE active=1"
        q += " ORDER BY title"
        return conn.execute(q).fetchall()

def delete_dispatcher_group(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM dispatcher_groups WHERE chat_id=?", (chat_id,))

def get_all_dispatcher_chat_ids() -> list[int]:
    """Возвращает список chat_id всех активных групп диспетчеров."""
    groups = get_dispatcher_groups(active_only=True)
    return [g["chat_id"] for g in groups]


# ── CRUD шаблоны ──────────────────────────────────────────────
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

# ── CRUD расписания ───────────────────────────────────────────
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
        raise ValueError(f"Invalid schedule format: '{expr}'. Use 09:00, */4h or */10m")
    hh, mm = map(int, t.split(":"))
    r = {"type": "daily", "time": dtime(hour=hh, minute=mm)}
    if extra:
        wd = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
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
    doc_id = s["doc_id"] if "doc_id" in s.keys() else None
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
            log.warning(f"Schedule #{sid} → {cid}: {e}")


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
        app.job_queue.run_daily(
            job_send_scheduled,
            time=cron["time"].replace(tzinfo=ZoneInfo(TRUCK_TIMEZONE)),
            days=days, data=data, name=name
        )
    log.info(f"Schedule: {name}")


def unregister_schedule(app, sid):
    for job in app.job_queue.get_jobs_by_name(f"sched_{sid}"):
        job.schedule_removal()


def register_all_schedules(app):
    for s in get_schedules(active_only=True):
        try:
            register_schedule(app, dict(s))
        except Exception as e:
            log.warning(f"Schedule #{s['id']} пропущено: {e}")
            delete_schedule(s["id"])


# ══════════════════════════════════════════════════════════════
# ПОГОДА
# ══════════════════════════════════════════════════════════════
WEATHER_EMOJI = {
    "Clear": "☀️", "Clouds": "☁️", "Rain": "🌧️",
    "Drizzle": "🌦️", "Thunderstorm": "⛈️", "Snow": "❄️",
    "Mist": "🌫️", "Fog": "🌫️", "Haze": "🌫️",
}
SEVERE_CONDITIONS = {"Thunderstorm", "Tornado", "Squall", "Snow", "Blizzard"}

# ── Пороги опасности для фур ─────────────────────────────────
WIND_DANGER_MPH     = 40   # боковой ветер — риск опрокидывания
WIND_CAUTION_MPH    = 25   # сильный ветер — снизить скорость
VISIBILITY_DANGER   = 0.25 # мили — почти нулевая видимость
VISIBILITY_CAUTION  = 1.0  # мили — плохая видимость
FREEZE_TEMP_F       = 35   # °F — риск гололёда
HEAVY_RAIN_MM       = 10   # мм/3ч — сильный ливень


def analyze_truck_hazards(w: dict) -> list[str]:
    """
    Анализирует погодные данные и возвращает список предупреждений для фуры.
    Возвращает список строк — каждая строка одно предупреждение.
    """
    alerts = []
    main   = w.get("main", {})
    wind   = w.get("wind", {})
    cond   = w.get("weather", [{}])[0]
    vis    = w.get("visibility", 10000)  # метры
    rain   = w.get("rain", {}).get("3h", 0)
    snow   = w.get("snow", {}).get("3h", 0)

    temp_f    = main.get("temp", 50)
    wind_spd  = wind.get("speed", 0)       # mph (imperial)
    wind_gust = wind.get("gust", wind_spd) # mph
    cond_main = cond.get("main", "")
    vis_miles = vis / 1609.34              # метры → мили

    # 1. Критичный ветер — риск опрокидывания фуры
    if wind_gust >= WIND_DANGER_MPH:
        alerts.append(
            f"🚨 CRITICAL: wind gusts {wind_gust:.0f} mph — high rollover risk! Stop immediately."
        )
    elif wind_spd >= WIND_DANGER_MPH:
        alerts.append(
            f"🚨 DANGEROUS: crosswind {wind_spd:.0f} mph — stay away from barriers, reduce speed."
        )
    elif wind_spd >= WIND_CAUTION_MPH or wind_gust >= WIND_CAUTION_MPH:
        alerts.append(
            f"⚠️ Strong wind {wind_spd:.0f} mph (gusts {wind_gust:.0f} mph) — reduce speed."
        )

    # 2. Видимость
    if vis_miles <= VISIBILITY_DANGER:
        alerts.append(
            f"🚨 КРИТИЧНО: видимость {vis_miles:.2f} мили — "
            f"почти ноль. Включите аварийку, съедьте на обочину."
        )
    elif vis_miles <= VISIBILITY_CAUTION:
        alerts.append(
            f"⚠️ Плохая видимость: {vis_miles:.1f} мили — "
            f"включите фары, увеличьте дистанцию."
        )

    # 3. Гололёд — температура около нуля + осадки
    if temp_f <= FREEZE_TEMP_F and cond_main in ("Rain", "Drizzle", "Snow", "Sleet"):
        alerts.append(
            f"🧊 ГОЛОЛЁД: температура {temp_f:.0f}°F + осадки — "
            f"дорога обледенела. Скорость не выше 35 mph."
        )
    elif temp_f <= FREEZE_TEMP_F and cond_main == "Clouds":
        alerts.append(
            f"🌡 Температура {temp_f:.0f}°F — возможен чёрный лёд "
            f"на мостах и развязках."
        )

    # 4. Снег
    if snow > 0:
        if snow >= 5:
            alerts.append(
                f"❄️ СИЛЬНЫЙ снегопад: {snow:.1f} мм/3ч — "
                f"видимость резко падает, цепи на колёса."
            )
        else:
            alerts.append(
                f"❄️ Снегопад: {snow:.1f} мм/3ч — "
                f"снизьте скорость, увеличьте дистанцию."
            )

    # 5. Сильный дождь
    if rain >= HEAVY_RAIN_MM:
        alerts.append(
            f"🌧️ Сильный ливень: {rain:.1f} мм/3ч — "
            f"аквапланирование, скорость не выше 45 mph."
        )
    elif rain > 0 and cond_main == "Rain":
        alerts.append(
            f"🌧️ Rain — increase following distance to 4 seconds."
        )

    # 6. Гроза
    if cond_main == "Thunderstorm":
        alerts.append(
            "⛈️ ГРОЗА — немедленно остановитесь в безопасном месте. "
            "Не ехать во время грозы на высоком профиле."
        )

    # 7. Торнадо / шквал
    if cond_main in ("Tornado", "Squall"):
        alerts.append(
            "🌪️ ТОРНАДО/ШКВАЛ — экстренная остановка! "
            "Покиньте кабину, лягте в низину."
        )

    # 8. Туман
    if cond_main in ("Fog", "Mist", "Haze") and vis_miles < 0.5:
        alerts.append(
            f"🌫️ Густой туман — включите противотуманки, "
            f"скорость не выше 30 mph."
        )

    return alerts


def format_truck_alerts(alerts: list[str]) -> str:
    """Форматирует список алертов в текст."""
    if not alerts:
        return ""
    return "\n".join(alerts)


def format_weather_city(lat: float, lon: float, label: str = "") -> str:
    """Текущая погода + прогноз 3 дня + алерты для фуры."""
    unit  = "°F" if WEATHER_UNITS == "imperial" else "°C"
    speed = "mph" if WEATHER_UNITS == "imperial" else "м/с"

    if not WEATHER_API_KEY:
        return "⚠️ WEATHER_API_KEY не настроен."

    w = get_weather_data(lat, lon)
    if not w:
        return f"❌ Could not get weather for {label or 'города'}"

    main  = w["main"]
    wind  = w["wind"]
    cond  = w["weather"][0]
    emoji = WEATHER_EMOJI.get(cond["main"], "🌡️")
    name  = w.get("name", "?")
    vis_m = w.get("visibility", 10000)
    vis_miles = vis_m / 1609.34
    gust  = wind.get("gust", 0)
    header = f"{label} — {name}" if label else name

    lines = [
        f"{emoji} {header}",
        f"🌡 {main['temp']:.0f}{unit}, feels like {main['feels_like']:.0f}{unit}",
        f"💧 Humidity: {main['humidity']}%",
        f"💨 Wind: {wind['speed']:.1f} {speed}" +
            (f", gusts {gust:.0f} {speed}" if gust > wind["speed"] + 5 else ""),
        f"👁 Visibility: {vis_miles:.1f} mi" if vis_miles < 5 else f"🌥 {cond['description'].capitalize()}",
    ]

    # Алерты для фуры
    truck_alerts = analyze_truck_hazards(w)
    if truck_alerts:
        lines.append("")
        lines.append("🚛 DRIVER WARNINGS:")
        lines.extend(truck_alerts)

    lines.append("")
    lines.append("📅 3-Day Forecast:")

    fc = get_forecast_data(lat, lon)
    if fc:
        seen = set()
        for item in fc["list"]:
            dt  = datetime.fromtimestamp(item["dt"], tz=ZoneInfo(TRUCK_TIMEZONE))
            day = dt.strftime("%a %d.%m")
            if day in seen:
                continue
            seen.add(day)
            if len(seen) > 3:
                break
            em    = WEATHER_EMOJI.get(item["weather"][0]["main"], "🌡️")
            w_day = item.get("wind", {})
            gust_day = w_day.get("gust", 0)
            wind_day = w_day.get("speed", 0)
            # Мини-алерт в прогнозе
            day_alert = ""
            if gust_day >= WIND_DANGER_MPH or wind_day >= WIND_DANGER_MPH:
                day_alert = " ⚠️ветер"
            if item["main"]["temp_min"] <= FREEZE_TEMP_F:
                day_alert += " 🧊лёд"
            if item["weather"][0]["main"] == "Thunderstorm":
                day_alert += " ⛈"
            lines.append(
                f"{em} {day}: {item['main']['temp_max']:.0f}/{item['main']['temp_min']:.0f}{unit}"
                f" — {item['weather'][0]['description']}{day_alert}"
            )
    else:
        lines.append("(forecast unavailable)")

    return "\n".join(lines)


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return _json.loads(r.read())


def geocode_city(city: str) -> dict | None:
    """
    Геокодирует город через OpenWeatherMap weather API.
    Возвращает {lat, lon, name} или None.
    """
    if not WEATHER_API_KEY:
        log.warning("WEATHER_API_KEY не задан")
        return None
    # Убираем мусор из названия города
    city = city.strip().strip("📦🟡📍🔰🛑").strip()
    if len(city) < 2:
        return None
    try:
        url = (f"https://api.openweathermap.org/data/2.5/weather"
               f"?q={urllib.parse.quote(city)}&appid={WEATHER_API_KEY}&units={WEATHER_UNITS}")
        data = _fetch_json(url)
        return {
            "lat": data["coord"]["lat"],
            "lon": data["coord"]["lon"],
            "name": data.get("name", city),
        }
    except urllib.error.HTTPError as e:
        log.warning(f"Geocoding «{city[:40]}»: HTTP {e.code}")
    except Exception as e:
        log.warning(f"Geocoding «{city[:40]}»: {e}")
    return None


def get_weather_data(lat: float, lon: float) -> dict | None:
    """Получает текущую погоду по координатам."""
    if not WEATHER_API_KEY:
        return None
    try:
        url = (f"https://api.openweathermap.org/data/2.5/weather"
               f"?lat={lat}&lon={lon}&appid={WEATHER_API_KEY}&units={WEATHER_UNITS}&lang=en")
        return _fetch_json(url)
    except Exception as e:
        log.warning(f"Weather ({lat},{lon}): {e}")
        return None


def get_forecast_data(lat: float, lon: float) -> dict | None:
    """Получает прогноз на 3 дня по координатам."""
    if not WEATHER_API_KEY:
        return None
    try:
        url = (f"https://api.openweathermap.org/data/2.5/forecast"
               f"?lat={lat}&lon={lon}&appid={WEATHER_API_KEY}&units={WEATHER_UNITS}&lang=en&cnt=24")
        return _fetch_json(url)
    except Exception as e:
        log.warning(f"Forecast ({lat},{lon}): {e}")
        return None


async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unified weather command — /liveweather."""
    await update.message.reply_text(
        "🌤 Weather — choose mode:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📍 City weather", callback_data="wx_city")],
            [InlineKeyboardButton("🗺 Route A → B",   callback_data="wx_route")],
            [InlineKeyboardButton("📡 Live tracking", callback_data="wx_live")],
        ])
    )
    return WX_CHOICE


async def wx_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор режима погоды."""
    q = update.callback_query
    await q.answer()
    await q.message.delete()

    if q.data == "wx_city":
        await q.message.chat.send_message(
            "🌤 Введите название города:\n\n"
            "Например: <code>Chicago</code> или <code>New York</code>",
            parse_mode="HTML"
        )
        return WX_CITY

    elif q.data == "wx_route":
        await q.message.chat.send_message(
            "🗺 Введите точку отправления:\n\n"
            "Например: <code>San Bernardino, CA</code>",
            parse_mode="HTML"
        )
        return WX_ORIGIN

    elif q.data == "wx_live":
        await q.message.chat.send_message(
            "📡 Enter route using /:\n\n"
            "<code>New York / Cleveland / Chicago</code>\n\n"
            "Then enable: 📎 Paperclip → Location → Share Live Location",
            parse_mode="HTML"
        )
        return WX_ROUTE

    return ConversationHandler.END


async def wx_get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Погода в одном городе."""
    city = update.message.text.strip()
    msg  = await update.message.reply_text(f"⏳ Getting weather for {city}...")
    geo  = geocode_city(city)
    if not geo:
        await msg.edit_text(
            f"❌ Город «{city}» не найден.\n"
            "Попробуйте на английском: Chicago, New York"
        )
        return ConversationHandler.END
    text = format_weather_city(geo["lat"], geo["lon"])
    if ANTHROPIC_API_KEY:
        w = get_weather_data(geo["lat"], geo["lon"])
        if w and w["weather"][0]["main"] in SEVERE_CONDITIONS:
            advice = await claude_location_advice(
                geo["name"], w["weather"][0]["main"],
                w["main"]["temp"], w["wind"]["speed"]
            )
            if advice:
                text += f"\n\n🤖 Совет:\n{advice}"
    await msg.edit_text(text)
    return ConversationHandler.END


async def wx_get_origin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает точку А маршрута."""
    context.user_data["wx_origin"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Start: {context.user_data['wx_origin']}\n\n"
        "Now enter destination:\n"
        "Example: <code>Teterboro, NJ</code>",
        parse_mode="HTML"
    )
    return WX_DEST


async def wx_get_dest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает точку Б и строит маршрут."""
    origin  = context.user_data.pop("wx_origin", "")
    dest    = update.message.text.strip()
    chat_id = update.effective_chat.id
    msg     = await update.message.reply_text(f"🔍 Building route {origin} → {dest}...")

    cities = get_route_cities(origin, dest)
    if not cities:
        city_dicts = []
        for c in [origin, dest]:
            geo = geocode_city(c)
            if geo:
                city_dicts.append(geo)
        if len(city_dicts) >= 2:
            await msg.edit_text(f"🗺 {origin} → {dest}\n\nGetting weather...")
            await send_route_weather(context.bot, chat_id, city_dicts, origin, dest)
        else:
            await msg.edit_text("❌ Cities not found. Check the names.")
    else:
        await msg.edit_text(f"🗺 {origin} → {dest}\nStops: {len(cities)}\nGetting weather...")
        await send_route_weather(context.bot, chat_id, cities, origin, dest)
    return ConversationHandler.END


async def wx_get_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает маршрут для живого отслеживания."""
    chat_id   = update.effective_chat.id
    cities_raw = [c.strip() for c in update.message.text.split("/") if c.strip()]
    if len(cities_raw) < 2:
        await update.message.reply_text(
            "Нужно минимум 2 города.\nПример: <code>New York / Chicago</code>",
            parse_mode="HTML"
        )
        return WX_ROUTE
    await update.message.reply_text(f"📋 Route: {' → '.join(cities_raw)}\nGetting weather...")
    city_dicts = [geo for c in cities_raw if (geo := geocode_city(c))]
    if len(city_dicts) >= 2:
        await send_route_weather(context.bot, chat_id, city_dicts, cities_raw[0], cities_raw[-1])
    else:
        await update.message.reply_text("❌ Could not find cities.")
    await context.bot.send_message(
        chat_id=chat_id,
        text="📎 Включите живую геолокацию:\nPaperclip → Location → Share Live Location"
    )
    return ConversationHandler.END


async def wx_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_weather_conv():
    """Единый ConversationHandler для всех режимов погоды."""
    return ConversationHandler(
        entry_points=[CommandHandler("liveweather", cmd_weather), CommandHandler("weather", cmd_weather)],
        states={
            WX_CHOICE: [CallbackQueryHandler(wx_choice, pattern=r"^wx_")],
            WX_CITY:   [MessageHandler(filters.TEXT & ~filters.COMMAND, wx_get_city)],
            WX_ORIGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, wx_get_origin)],
            WX_DEST:   [MessageHandler(filters.TEXT & ~filters.COMMAND, wx_get_dest)],
            WX_ROUTE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, wx_get_route)],
        },
        fallbacks=[CommandHandler("cancel", wx_cancel)],
        per_user=True,
        per_chat=False,
        per_message=False,
        allow_reentry=True,
    )



async def ask_claude(prompt: str, max_tokens: int = 500) -> str:
    """Базовый запрос к Claude API."""
    if not ANTHROPIC_API_KEY:
        return ""
    try:
        payload = _json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            data = _json.loads(r.read())
        return data["content"][0]["text"].strip()
    except Exception as e:
        log.warning(f"Claude API: {e}")
        return ""


async def claude_route_analysis(cities_weather: list[dict], origin: str, dest: str) -> str:
    """Claude analyzing погоду и truck hazards по всему маршруту."""
    if not ANTHROPIC_API_KEY:
        return ""
    lines = []
    has_alerts = False
    for item in cities_weather:
        alert_str = "; ".join(item.get("alerts", [])) or "no hazards"
        if item.get("alerts"):
            has_alerts = True
        lines.append(
            f"- {item['label']} ({item['city']}): "
            f"{item['condition']}, {item['temp']:.0f}F, "
            f"wind {item['wind']:.1f} mph (gusts {item.get('wind_gust', 0):.0f}), "
            f"visibility {item.get('visibility', 10):.1f} mi. "
            f"Hazards: {alert_str}"
        )
    summary = "\n".join(lines)
    prompt = (
        f"You are a safety advisor for a long-haul truck driver from {origin} to {dest}.\n"
        f"Weather and hazard data per city:\n{summary}\n\n"
        "Based on this data provide in Russian:\n"
        "1. Overall route safety: ✅ Safe / ⚠️ Caution / 🚨 Dangerous\n"
        "2. Most dangerous section (if any)\n"
        "3. Specific advice: speed limits, when to stop, what to watch for\n"
        "4. Best time window to drive if weather is bad\n"
        "Be concise, practical, use emojis. Max 10 lines."
    )
    return await ask_claude(prompt, max_tokens=600)


async def claude_location_advice(city: str, condition: str, temp: float, wind: float) -> str:
    """Совет от Claude при обновлении геолокации."""
    if not ANTHROPIC_API_KEY:
        return ""
    prompt = (
        f"Truck driver near {city}. Weather: {condition}, {temp:.0f}F, wind {wind:.1f} mph. "
        "Brief safety tip (2-3 lines, emojis, Russian). "
        "If weather is fine respond exactly: OK"
    )
    result = await ask_claude(prompt, max_tokens=150)
    return "" if result.strip() == "OK" else result



# ══════════════════════════════════════════════════════════════
# GOOGLE MAPS + ПОГОДА ПО МАРШРУТУ
# ══════════════════════════════════════════════════════════════
def get_route_cities(origin: str, dest: str) -> list[dict] | None:
    if not GOOGLE_MAPS_KEY:
        return None
    try:
        params = urllib.parse.urlencode({"origin": origin, "destination": dest, "key": GOOGLE_MAPS_KEY})
        data = _fetch_json(f"https://maps.googleapis.com/maps/api/directions/json?{params}")
        if data["status"] != "OK":
            return None
        leg = data["routes"][0]["legs"][0]
        cities = []
        seen = set()

        def add(name, lat, lon):
            k = name.lower().strip()
            if k and k not in seen:
                seen.add(k)
                cities.append({"name": name, "lat": lat, "lon": lon})

        add(origin, leg["start_location"]["lat"], leg["start_location"]["lng"])
        for step in leg["steps"]:
            for city, state in re.findall(r"([A-Z][a-zA-Z\s]+),\s*([A-Z]{2})", step.get("html_instructions", "")):
                city = city.strip()
                if len(city) > 2:
                    add(f"{city}, {state}", step["end_location"]["lat"], step["end_location"]["lng"])
        add(dest, leg["end_location"]["lat"], leg["end_location"]["lng"])

        return cities if len(cities) >= 2 else [
            {"name": origin, "lat": leg["start_location"]["lat"], "lon": leg["start_location"]["lng"]},
            {"name": dest, "lat": leg["end_location"]["lat"], "lon": leg["end_location"]["lng"]},
        ]
    except Exception as e:
        log.warning(f"Google Maps: {e}")
        return None


async def send_route_weather(bot, chat_id: int, cities: list[dict], origin: str, dest: str):
    """Отправляет погоду по всем городам маршрута + анализ от Claude."""
    cities_weather_data = []  # для Claude

    for i, city in enumerate(cities):
        label = "🚦 Start" if i == 0 else ("🏁 Finish" if i == len(cities) - 1 else f"📍 Stop {i}")
        # Отправляем погоду + прогноз 3 дня + truck alerts
        text = format_weather_city(city["lat"], city["lon"], label)
        await bot.send_message(chat_id=chat_id, text=text)
        # Собираем данные для Claude
        w = get_weather_data(city["lat"], city["lon"])
        if w:
            alerts = analyze_truck_hazards(w)
            cities_weather_data.append({
                "label": label,
                "city": w.get("name", city["name"]),
                "condition": w["weather"][0]["main"],
                "temp": w["main"]["temp"],
                "wind": w["wind"].get("speed", 0),
                "wind_gust": w["wind"].get("gust", 0),
                "humidity": w["main"]["humidity"],
                "visibility": w.get("visibility", 10000) / 1609.34,
                "alerts": alerts,
            })

    # Claude analyzing весь маршрут и даёт развёрнутый совет
    if ANTHROPIC_API_KEY and cities_weather_data:
        thinking_msg = await bot.send_message(chat_id=chat_id, text="🤖 Claude analyzing route...")
        advice = await claude_route_analysis(cities_weather_data, origin, dest)
        if advice:
            await thinking_msg.edit_text(f"🤖 Анализ маршрута от Claude:\n\n{advice}")
        else:
            await thinking_msg.delete()

    await bot.send_message(
        chat_id=chat_id,
        text="✅ Done! Use /liveweather to track weather en route."
    )


# ══════════════════════════════════════════════════════════════
# ЖИВАЯ ГЕОЛОКАЦИЯ
# ══════════════════════════════════════════════════════════════
live_locations: dict[int, dict] = {}
DISTANCE_THRESHOLD_KM = 50


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


async def handle_live_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.edited_message
    if not msg or not msg.location:
        return
    user_id = msg.from_user.id
    chat_id = msg.chat_id
    lat, lon = msg.location.latitude, msg.location.longitude

    if getattr(msg.location, "live_period", None) is None and user_id in live_locations:
        del live_locations[user_id]
        await context.bot.send_message(chat_id=chat_id, text="📍 Tracking stopped.")
        return

    prev = live_locations.get(user_id)
    if not prev:
        live_locations[user_id] = {"lat": lat, "lon": lon, "last_cond": None, "chat_id": chat_id}
        try:
            w = _fetch_json(_weather_url(lat, lon))
            live_locations[user_id]["last_cond"] = w["weather"][0]["main"]
            text = "🚛 Начало отслеживания\n\n" + format_weather_city(lat, lon)
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            pass
        return

    dist = haversine_km(prev["lat"], prev["lon"], lat, lon)
    live_locations[user_id].update({"lat": lat, "lon": lon})

    if dist < DISTANCE_THRESHOLD_KM:
        return

    try:
        w = _fetch_json(_weather_url(lat, lon))
        new_cond = w["weather"][0]["main"]
        severe = new_cond in SEVERE_CONDITIONS
        prev_cond = prev.get("last_cond")

        if new_cond != prev_cond or severe:
            live_locations[user_id]["last_cond"] = new_cond
            text = f"📍 Обновление погоды (+{dist:.0f} км)\n\n" + format_weather_city(lat, lon)
            await context.bot.send_message(chat_id=chat_id, text=text)

            if ANTHROPIC_API_KEY and (severe or new_cond != prev.get("last_cond")):
                city_name = w.get("name", "текущее местоположение")
                advice = await claude_location_advice(
                    city_name, new_cond,
                    w["main"]["temp"], w["wind"]["speed"]
                )
                if advice:
                    await context.bot.send_message(chat_id=chat_id, text=f"🤖 Совет от Claude:\n\n{advice}")
    except Exception as e:
        log.warning(f"Live location weather: {e}")


# ══════════════════════════════════════════════════════════════
# АВТОДЕТЕКТ TRIP ID
# ══════════════════════════════════════════════════════════════
def normalize_text(text: str) -> str:
    """Нормализует unicode bold/italic символы в обычные ASCII."""
    result = []
    offsets = [
        (0x1D400, 0x1D419, 65), (0x1D41A, 0x1D433, 97),
        (0x1D434, 0x1D44D, 65), (0x1D44E, 0x1D467, 97),
        (0x1D468, 0x1D481, 65), (0x1D482, 0x1D49B, 97),
        (0x1D5D4, 0x1D5ED, 65), (0x1D5EE, 0x1D607, 97),
        (0x1D608, 0x1D621, 65), (0x1D622, 0x1D63B, 97),
        (0x1D63C, 0x1D655, 65), (0x1D656, 0x1D66F, 97),
    ]
    for ch in unicodedata.normalize("NFKD", text):
        cp = ord(ch)
        if 0x1D400 <= cp <= 0x1D7FF:
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


def extract_cities_from_trip(text: str) -> list[str]:
    """Извлекает города вида 'City, ST' из текста Trip ID."""
    # Нормализуем переносы строк — убираем их внутри потенциальных названий
    text = re.sub(r"\s*\n\s*", " ", text)
    pattern = re.compile(r"([A-Z][a-zA-Z ]{2,25}),\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?")
    skip = {
        "Loaded", "Drop", "Preloaded", "Route", "Ave", "Blvd", "St", "Dr",
        "Tue", "Wed", "Thu", "Fri", "Mon", "Sat", "Sun",
        "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar",
        "Central Ave", "E Central Ave", "N Main St", "S Main St",
    }
    cities, seen = [], set()
    for city, state in pattern.findall(text):
        city = city.strip()
        # Убираем лишние слова в начале (E, N, S, W — стороны света)
        city = re.sub(r"^[NSEW]\s+", "", city).strip()
        if len(city) < 3 or city in skip:
            continue
        if re.match(r"^[A-Z]{2,4}\d+$", city):
            continue
        # Убираем если содержит слова улиц
        if any(w in city for w in ["Ave", "Blvd", "St ", "Dr ", "Rd ", "Hwy", "Route"]):
            continue
        key = f"{city}, {state}"
        if key not in seen:
            seen.add(key)
            cities.append(key)
    return cities


TRIP_KEYWORDS = ["Trip ID", "Loaded -", "Per mile", "Duration", "Preloaded"]


async def auto_detect_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Автодетект Trip ID сообщения."""
    msg = update.message
    if not msg:
        return
    raw = msg.text or msg.caption or ""
    if not raw:
        return

    clean = normalize_text(raw)

    if not any(kw.lower() in clean.lower() for kw in TRIP_KEYWORDS):
        return

    log.info(f"auto_detect_trip: Trip ID detected in chat {msg.chat_id}")

    context.bot_data[f"trip_{msg.message_id}"] = {
        "text": clean,
        "chat_id": update.effective_chat.id,
    }
    await msg.reply_text(
        "🚛 Вижу сообщение с маршрутом!\nОтправить погоду по всем точкам?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes", callback_data=f"autotrip_{msg.message_id}"),
            InlineKeyboardButton("❌ No", callback_data="autotrip_cancel"),
        ]])
    )


async def cb_autotrip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "autotrip_cancel":
        await q.message.delete()
        return

    msg_id = q.data.replace("autotrip_", "")
    saved = context.bot_data.get(f"trip_{msg_id}")
    if not saved:
        await q.message.edit_text("❌ Data expired. Please try again.")
        return

    cities = extract_cities_from_trip(saved["text"])
    if not cities:
        await q.message.edit_text("❌ Could not find cities in the message.")
        return

    await q.message.edit_text(f"📋 Route: {' → '.join(cities)}\n\nGetting weather...")
    chat_id = saved["chat_id"]

    city_dicts = []
    for city in cities:
        geo = geocode_city(city)
        if geo:
            city_dicts.append(geo)

    if len(city_dicts) < 2:
        await context.bot.send_message(chat_id=chat_id, text="❌ Не удалось геокодировать города.")
        return

    await send_route_weather(context.bot, chat_id, city_dicts, cities[0], cities[-1])


# ══════════════════════════════════════════════════════════════
# МАРШРУТ А → Б (/routeweather)
# ══════════════════════════════════════════════════════════════
RW_ORIGIN = 100
RW_DEST = 101


async def cmd_routeweather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info(f"cmd_routeweather от {update.effective_user.id} в чате {update.effective_chat.id}")
    await update.message.reply_text(
        "🗺 Введите точку отправления:\n\n"
        "Например: <code>San Bernardino, CA</code>",
        parse_mode="HTML"
    )
    return RW_ORIGIN


async def rw_get_origin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rw_origin"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Старт: {context.user_data['rw_origin']}\n\n"
        "Now enter destination:\n"
        "Example: <code>Teterboro, NJ</code>",
        parse_mode="HTML"
    )
    return RW_DEST


async def rw_get_dest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    origin = context.user_data.pop("rw_origin", "")
    dest = update.message.text.strip()
    chat_id = update.effective_chat.id

    msg = await update.message.reply_text(f"🔍 Building route {origin} → {dest}...")

    # Пробуем Google Maps
    cities = get_route_cities(origin, dest)

    if not cities:
        # Без Google Maps — геокодируем только старт и финиш
        await msg.edit_text(f"📋 {origin} → {dest}\n\nGetting weather...")
        city_dicts = []
        for c in [origin, dest]:
            geo = geocode_city(c)
            if geo:
                city_dicts.append(geo)
        if len(city_dicts) >= 2:
            await send_route_weather(context.bot, chat_id, city_dicts, origin, dest)
        else:
            await context.bot.send_message(chat_id=chat_id, text="❌ Cities not found. Check the names.")
    else:
        await msg.edit_text(f"🗺 {origin} → {dest}\nStops: {len(cities)}\nGetting weather...")
        await send_route_weather(context.bot, chat_id, cities, origin, dest)

    return ConversationHandler.END


async def rw_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════
# /liveweather — маршрут + живая геолокация
# ══════════════════════════════════════════════════════════════
LW_ROUTE = 200


async def cmd_liveweather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🗺 Укажите маршрут:\n\n"
        "<code>New York / Cleveland / Chicago</code>\n\n"
        "Первый — промежуточные — последний.\n"
        "Или без промежуточных: <code>New York / Chicago</code>",
        parse_mode="HTML"
    )
    return LW_ROUTE


async def lw_get_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cities_raw = [c.strip() for c in update.message.text.split("/") if c.strip()]

    if len(cities_raw) < 2:
        await update.message.reply_text("Нужно минимум 2 города.\nПример: <code>New York / Chicago</code>", parse_mode="HTML")
        return LW_ROUTE

    await update.message.reply_text(f"📋 Route: {' → '.join(cities_raw)}\nGetting weather...")

    city_dicts = []
    for c in cities_raw:
        geo = geocode_city(c)
        if geo:
            city_dicts.append(geo)

    if len(city_dicts) >= 2:
        await send_route_weather(context.bot, chat_id, city_dicts, cities_raw[0], cities_raw[-1])
    else:
        await context.bot.send_message(chat_id=chat_id, text="❌ Could not find cities.")

    await context.bot.send_message(
        chat_id=chat_id,
        text="📎 Теперь включите живую геолокацию:\nPaperclip → Location → Share Live Location"
    )
    return ConversationHandler.END


async def lw_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════
# КЛАВИАТУРЫ И УТИЛИТЫ
# ══════════════════════════════════════════════════════════════
def is_op(uid, chat_id=None) -> bool:
    """Проверяет права оператора — по user_id или по группе."""
    if uid in OPERATOR_IDS:
        return True
    if chat_id and chat_id in OPERATOR_CHAT_IDS:
        return True
    return False


def is_op_update(update) -> bool:
    """Проверяет права оператора из объекта Update."""
    uid = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else 0
    return is_op(uid, chat_id)

def kb_op():
    return ReplyKeyboardMarkup([
        ["👥 Drivers", "📋 Templates"],
        ["🕐 Schedules", "📨 Broadcast"],
        ["📢 Dispatchers"],
    ], resize_keyboard=True)

def kb_back(cb="back_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data=cb)]])

def drivers_kb():
    rows = [[InlineKeyboardButton("📢 All drivers", callback_data="target_all")]]
    for d in get_all_drivers():
        rows.append([InlineKeyboardButton(f"👤 {d['name']}", callback_data=f"target_{d['chat_id']}")])
    return InlineKeyboardMarkup(rows)


# ══════════════════════════════════════════════════════════════
# СОСТОЯНИЯ ДИАЛОГОВ (операторские)
# ══════════════════════════════════════════════════════════════
(
    ST_DRV_NAME, ST_DRV_CHAT,
    ST_TPL_TITLE, ST_TPL_TEXT,
    ST_SCH_TITLE, ST_SCH_TEXT, ST_SCH_CRON, ST_SCH_TARGET,
    ST_BC_TEXT, ST_BC_TARGET,
    ST_DISP_CHAT, ST_DISP_TITLE,
    # Единая команда погоды
    WX_CHOICE, WX_CITY, WX_ORIGIN, WX_DEST, WX_ROUTE,
) = range(17)


# ── /start, /myid ────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type

    if is_op(uid, chat_id):
        # Оператор (лично или из группы диспетчеров) — показываем панель
        await update.message.reply_text("👨‍💼 Operator panel:", reply_markup=kb_op())
    elif chat_type in ("group", "supergroup"):
        # Группа водителя — только команды водителя
        await update.message.reply_text(
            "🚛 Trucking Bot\n\n"
            "Команды для водителя:\n"
            "/weather <город> — погода и прогноз на 3 дня\n"
            "/arrived — уведомить о прибытии\n"
            "/alarm — установить будильник\n"
            "/awake — подтвердить пробуждение\n"
            "/cancel — отменить текущее действие"
        )
    else:
        await update.message.reply_text(
            "🚛 Trucking Bot\n\n"
            "Используйте команды в вашей группе:\n"
            "/weather — погода\n"
            "/arrived — прибытие\n"
            "/alarm — будильник"
        )


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает ID текущего чата — для настройки группы диспетчеров."""
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(
        f"📢 Chat info:\n\n"
        f"Chat ID: <code>{chat.id}</code>\n"
        f"Title: {chat.title or chat.full_name or chr(8212)}\n"
        f"Type: {chat.type}\n\n"
        "Copy the ID and add it as a dispatcher group\n"
        "in operator panel → 📢 Dispatchers → ➕ Add group",
        parse_mode="HTML"
    )




async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"👤 Your ID: <code>{uid}</code>\n"
        f"Operator: {'✅' if is_op(uid) else '❌'}\n"
        f"OPERATOR_IDS: <code>{OPERATOR_IDS}</code>",
        parse_mode="HTML"
    )


# ── ВОДИТЕЛИ ─────────────────────────────────────────────────
async def sec_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_op_update(update): return
    drivers = get_all_drivers(active_only=False)
    rows = [[InlineKeyboardButton(
        ("✅ " if d["active"] else "❌ ") + d["name"],
        callback_data=f"drv_edit_{d['chat_id']}"
    )] for d in drivers]
    rows.append([InlineKeyboardButton("➕ Add driver", callback_data="drv_add")])
    text = "👥 Drivers:\n" + "\n".join(
        f"{'✅' if d['active'] else '❌'} {d['name']} ({d['chat_id']})" for d in drivers
    ) if drivers else "👥 No drivers yet."
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def cb_drv_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Enter driver name:")
    return ST_DRV_NAME


async def st_drv_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["drv_name"] = update.message.text.strip()
    await update.message.reply_text("Enter the driver group chat_id.\n\nHow to get it: add @RawDataBot to the group.")
    return ST_DRV_CHAT


async def st_drv_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Invalid format. Enter numeric ID:")
        return ST_DRV_CHAT
    name = context.user_data.pop("drv_name", "Водитель")
    if add_driver(cid, name):
        await update.message.reply_text(f"✅ Driver {name} added.", reply_markup=kb_op())
    else:
        await update.message.reply_text(f"⚠️ Driver with chat_id {cid} already exists.", reply_markup=kb_op())
    return ConversationHandler.END


async def cb_drv_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = int(q.data.split("_")[-1])
    d = get_driver(cid)
    if not d:
        await q.message.reply_text("Not found.")
        return
    lbl = "Деактивировать" if d["active"] else "Активировать"
    await q.message.reply_text(
        f"Водитель: {d['name']}\nЧат: {cid}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔄 {lbl}", callback_data=f"drv_toggle_{cid}")],
            [InlineKeyboardButton("🗑 Delete", callback_data=f"drv_del_{cid}")],
            [InlineKeyboardButton("◀️ Back", callback_data="nav_drivers")],
        ])
    )


async def cb_drv_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = int(q.data.split("_")[-1])
    d = get_driver(cid)
    if d:
        toggle_driver(cid, not d["active"])
        s = "activated ✅" if not d["active"] else "deactivated ❌"
        await q.message.reply_text(f"Водитель {d['name']} {s}.")


async def cb_drv_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = int(q.data.split("_")[-1])
    d = get_driver(cid)
    if d:
        delete_driver(cid)
        await q.message.reply_text(f"🗑 {d['name']} removed.")


# ── ШАБЛОНЫ ──────────────────────────────────────────────────
async def sec_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_op_update(update): return
    tpls = get_templates()
    rows = [[InlineKeyboardButton(t["title"], callback_data=f"tpl_view_{t['id']}")] for t in tpls]
    rows.append([InlineKeyboardButton("➕ New template", callback_data="tpl_add")])
    await update.message.reply_text("📋 Templates:", reply_markup=InlineKeyboardMarkup(rows))


async def cb_tpl_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tid = int(q.data.split("_")[-1])
    t = get_template(tid)
    if not t: return
    await q.message.reply_text(
        f"📋 {t['title']}\n\n{t['text']}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📨 Send", callback_data=f"tpl_send_{tid}")],
            [InlineKeyboardButton("🗑 Delete", callback_data=f"tpl_del_{tid}")],
            [InlineKeyboardButton("◀️ Back", callback_data="nav_templates")],
        ])
    )


async def cb_tpl_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Enter template name:")
    return ST_TPL_TITLE


async def st_tpl_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tpl_title"] = update.message.text.strip()
    await update.message.reply_text("Enter template text:")
    return ST_TPL_TEXT


async def st_tpl_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = context.user_data.pop("tpl_title", "")
    add_template(title, update.message.text.strip())
    await update.message.reply_text(f"✅ Шаблон «{title}» saved.", reply_markup=kb_op())
    return ConversationHandler.END


async def cb_tpl_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tid = int(q.data.split("_")[-1])
    t = get_template(tid)
    if t:
        delete_template(tid)
        await q.message.reply_text(f"🗑 «{t['title']}» removed.")


async def cb_tpl_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tid = int(q.data.split("_")[-1])
    t = get_template(tid)
    if not t: return ConversationHandler.END
    context.user_data["bc_text"] = t["text"]
    await q.message.reply_text(f"Шаблон: «{t['title']}»\n\nКому?", reply_markup=drivers_kb())
    return ST_BC_TARGET


# ── РАСПИСАНИЯ ────────────────────────────────────────────────
async def sec_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_op_update(update): return
    scheds = get_schedules()
    rows = [[InlineKeyboardButton(
        ("✅ " if s["active"] else "⏸ ") + f"{s['title']} ({s['cron_expr']})",
        callback_data=f"sch_view_{s['id']}"
    )] for s in scheds]
    rows.append([InlineKeyboardButton("➕ New schedule", callback_data="sch_add")])
    await update.message.reply_text("🕐 Schedules:", reply_markup=InlineKeyboardMarkup(rows))


async def cb_sch_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sid = int(q.data.split("_")[-1])
    s = get_schedule(sid)
    if not s: return
    tgt = "All drivers" if s["target"] == "all" else s["target"]
    lbl = "⏸ Pause" if s["active"] else "▶️ Resume"
    await q.message.reply_text(
        f"🕐 {s['title']}\nSchedule: {s['cron_expr']}\nRecipients: {tgt}\n\n{s['text'] or '(без текста)'}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(lbl, callback_data=f"sch_toggle_{sid}")],
            [InlineKeyboardButton("🗑 Delete", callback_data=f"sch_del_{sid}")],
            [InlineKeyboardButton("◀️ Back", callback_data="nav_schedules")],
        ])
    )


async def cb_sch_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Enter schedule name:")
    return ST_SCH_TITLE


async def st_sch_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sch_title"] = update.message.text.strip()
    await update.message.reply_text("Enter notification text (or send photo/file):")
    return ST_SCH_TEXT


async def st_sch_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        context.user_data["sch_photo"] = update.message.photo[-1].file_id
        context.user_data["sch_text"] = update.message.caption or ""
    elif update.message.document:
        context.user_data["sch_doc"] = update.message.document.file_id
        context.user_data["sch_text"] = update.message.caption or ""
    else:
        context.user_data["sch_text"] = update.message.text.strip()
    await update.message.reply_text(
        "Enter schedule:\n\n"
        "<code>09:00</code> — every day\n"
        "<code>08:00|mon,wed,fri</code> — Mon, Wed, Fri\n"
        "<code>09:00|1</code> — first week of month\n"
        "<code>*/4h</code> — every 4 hours\n"
        "<code>*/10m</code> — every 10 minutes",
        parse_mode="HTML"
    )
    return ST_SCH_CRON


async def st_sch_cron(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sch_cron"] = update.message.text.strip()
    await update.message.reply_text("Who to send to?", reply_markup=drivers_kb())
    return ST_SCH_TARGET


async def st_sch_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
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
    await q.message.reply_text("✅ Schedule created.", reply_markup=kb_op())
    return ConversationHandler.END


async def cb_sch_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sid = int(q.data.split("_")[-1])
    s = get_schedule(sid)
    if not s: return
    new_active = 0 if s["active"] else 1
    update_schedule(sid, active=new_active)
    if new_active:
        register_schedule(context.application, dict(get_schedule(sid)))
        await q.message.reply_text(f"▶️ «{s['title']}» resumed.")
    else:
        unregister_schedule(context.application, sid)
        await q.message.reply_text(f"⏸ «{s['title']}» paused.")


async def cb_sch_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sid = int(q.data.split("_")[-1])
    s = get_schedule(sid)
    if s:
        unregister_schedule(context.application, sid)
        delete_schedule(sid)
        await q.message.reply_text(f"🗑 «{s['title']}» removed.")


# ── РАССЫЛКА ─────────────────────────────────────────────────
async def sec_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_op_update(update): return ConversationHandler.END
    await update.message.reply_text("📨 Enter text (or send photo/file with caption):")
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
    await update.message.reply_text("Who to send to?", reply_markup=drivers_kb())
    return ST_BC_TARGET


async def st_bc_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    target = q.data.replace("target_", "")
    text = context.user_data.pop("bc_text", "")
    photo = context.user_data.pop("bc_photo", None)
    doc = context.user_data.pop("bc_doc", None)
    chat_ids = [d["chat_id"] for d in get_all_drivers()] if target == "all" else [int(target)]
    sent = 0
    for cid in chat_ids:
        try:
            if photo:
                await context.bot.send_photo(chat_id=cid, photo=photo, caption=text)
            elif doc:
                await context.bot.send_document(chat_id=cid, document=doc, caption=text)
            else:
                await context.bot.send_message(chat_id=cid, text=text)
            log_send(cid, text)
            sent += 1
        except Exception as e:
            log.warning(f"Broadcast → {cid}: {e}")
    await q.message.reply_text(f"✅ Sent: {sent}/{len(chat_ids)}", reply_markup=kb_op())
    return ConversationHandler.END


# ── Уведомление групп диспетчеров ────────────────────────────
async def notify_dispatchers(bot, text: str) -> int:
    """Отправляет сообщение во все группы диспетчеров из БД."""
    groups = get_all_dispatcher_chat_ids()
    if not groups and DISPATCHER_GROUP_ID:
        groups = [DISPATCHER_GROUP_ID]
    sent = 0
    for chat_id in groups:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            sent += 1
        except Exception as e:
            log.warning(f"Dispatcher notification ({chat_id}): {e}")
    return sent


# ── НАВИГАЦИЯ ─────────────────────────────────────────────────
async def cb_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "back_main":
        await q.message.reply_text("Main menu:", reply_markup=kb_op())
    elif q.data == "nav_drivers":
        drivers = get_all_drivers(active_only=False)
        rows = [[InlineKeyboardButton(("✅ " if d["active"] else "❌ ") + d["name"], callback_data=f"drv_edit_{d['chat_id']}")] for d in drivers]
        rows.append([InlineKeyboardButton("➕ Add", callback_data="drv_add")])
        await q.message.reply_text("👥 Drivers:", reply_markup=InlineKeyboardMarkup(rows))
    elif q.data == "nav_templates":
        tpls = get_templates()
        rows = [[InlineKeyboardButton(t["title"], callback_data=f"tpl_view_{t['id']}")] for t in tpls]
        rows.append([InlineKeyboardButton("➕ New", callback_data="tpl_add")])
        await q.message.reply_text("📋 Templates:", reply_markup=InlineKeyboardMarkup(rows))
    elif q.data == "nav_dispatchers":
        groups = get_dispatcher_groups(active_only=False)
        rows = [[InlineKeyboardButton(f"📢 {g['title']}", callback_data=f"disp_del_{g['chat_id']}")] for g in groups]
        rows.append([InlineKeyboardButton("➕ Add", callback_data="disp_add")])
        await q.message.reply_text("📢 Dispatcher groups:", reply_markup=InlineKeyboardMarkup(rows))
    elif q.data == "nav_schedules":
        scheds = get_schedules()
        rows = [[InlineKeyboardButton(("✅ " if s["active"] else "⏸ ") + s["title"], callback_data=f"sch_view_{s['id']}")] for s in scheds]
        rows.append([InlineKeyboardButton("➕ New", callback_data="sch_add")])
        await q.message.reply_text("🕐 Schedules:", reply_markup=InlineKeyboardMarkup(rows))


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.", reply_markup=kb_op())
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════
# СБОРКА ConversationHandler-ов
# ══════════════════════════════════════════════════════════════
def build_routeweather_conv():
    """Маршрут А → Б."""
    return ConversationHandler(
        entry_points=[CommandHandler("routeweather", cmd_routeweather)],
        states={
            RW_ORIGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, rw_get_origin)],
            RW_DEST:   [MessageHandler(filters.TEXT & ~filters.COMMAND, rw_get_dest)],
        },
        fallbacks=[CommandHandler("cancel", rw_cancel)],
        per_user=True,
        per_chat=False,
        per_message=False,
        allow_reentry=True,
    )


def build_liveweather_conv():
    """/liveweather — маршрут для живой геолокации."""
    return ConversationHandler(
        entry_points=[CommandHandler("liveweather", cmd_liveweather)],
        states={
            LW_ROUTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, lw_get_route)],
        },
        fallbacks=[CommandHandler("cancel", lw_cancel)],
        per_user=True,
        per_chat=False,
        per_message=False,
        allow_reentry=True,
    )


def build_operator_conv():
    """Операторские диалоги: водители, шаблоны, расписания, рассылка."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_drv_add,   pattern="^drv_add$"),
            CallbackQueryHandler(cb_tpl_add,   pattern="^tpl_add$"),
            CallbackQueryHandler(cb_tpl_send,  pattern=r"^tpl_send_\d+$"),
            CallbackQueryHandler(cb_sch_add,   pattern="^sch_add$"),
            CallbackQueryHandler(cb_disp_add,  pattern="^disp_add$"),
            MessageHandler(filters.Regex("^📨 Broadcast$"), sec_broadcast),
        ],
        states={
            ST_DRV_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, st_drv_name)],
            ST_DRV_CHAT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, st_drv_chat)],
            ST_TPL_TITLE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, st_tpl_title)],
            ST_TPL_TEXT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, st_tpl_text)],
            ST_SCH_TITLE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, st_sch_title)],
            ST_SCH_TEXT:   [MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, st_sch_text)],
            ST_SCH_CRON:   [MessageHandler(filters.TEXT & ~filters.COMMAND, st_sch_cron)],
            ST_SCH_TARGET: [CallbackQueryHandler(st_sch_target, pattern=r"^target_")],
            ST_BC_TEXT:    [MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, st_bc_text)],
            ST_BC_TARGET:  [CallbackQueryHandler(st_bc_target, pattern=r"^target_")],
            ST_DISP_CHAT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, st_disp_chat)],
            ST_DISP_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_disp_title)],
        },
        fallbacks=[CommandHandler("cancel", conv_cancel)],
        per_user=True,
        per_chat=False,
        per_message=False,
    )


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════
# КОМАНДА /arrived — ПРИБЫТИЕ ВОДИТЕЛЯ
# ══════════════════════════════════════════════════════════════
# Хранилище ожидающих подтверждений:
# { job_name: {"user_id", "chat_id", "driver_name", "msg_id", "confirmed"} }
arrived_pending: dict[str, dict] = {}


async def cmd_arrived(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Водитель сообщает о прибытии. Запускается таймер ожидания ответа диспетчера."""
    user_id  = update.effective_user.id
    chat_id  = update.effective_chat.id
    driver   = update.effective_user.full_name or f"Водитель {user_id}"

    # Доп. информация из аргументов команды (опционально)
    location_note = " ".join(context.args) if context.args else ""
    location_text = f"\n📍 {location_note}" if location_note else ""

    # Отправляем уведомление в группу
    arrive_text = (
        f"🏁 {driver} прибыл на место!{location_text}\n\n"
        f"⏳ Ожидаю подтверждения от диспетчера...\n"
        f"Dispatcher: нажмите кнопку ниже или ответьте на сообщение."
    )
    job_name = f"arrived_{user_id}_{chat_id}"
    sent_msg = await update.message.reply_text(
        arrive_text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm arrival", callback_data=f"confirm_arrived_{job_name}"),
        ]])
    )

    # Сохраняем данные
    arrived_pending[job_name] = {
        "user_id":     user_id,
        "chat_id":     chat_id,
        "driver_name": driver,
        "msg_id":      sent_msg.message_id,
        "confirmed":   False,
        "location":    location_note,
    }

    # Запускаем таймер эскалации
    context.job_queue.run_once(
        job_arrived_escalate,
        when=ARRIVED_TIMEOUT,
        data={"job_name": job_name},
        name=job_name,
    )

    timeout_min = ARRIVED_TIMEOUT // 60
    timeout_sec = ARRIVED_TIMEOUT % 60
    time_label = f"{timeout_min} мин" if timeout_sec == 0 else f"{timeout_min} мин {timeout_sec} сек"
    await update.message.reply_text(
        f"⏱ Таймер запущен. Если диспетчер не ответит за {time_label} — "
        f"группа диспетчеров будет уведомлена."
    )
    log.info(f"Arrived: {driver} ({user_id}) в чате {chat_id}, таймер {ARRIVED_TIMEOUT}с")


async def job_arrived_escalate(context: ContextTypes.DEFAULT_TYPE):
    """Джоб: если нет подтверждения — уведомляем группу диспетчеров."""
    job_name = context.job.data["job_name"]
    data = arrived_pending.get(job_name)

    if not data or data["confirmed"]:
        return  # уже подтверждено

    driver   = data["driver_name"]
    chat_id  = data["chat_id"]
    location = data["location"]
    location_text = f"\n📍 {location}" if location else ""

    # Уведомляем в исходной группе
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"⚠️ Внимание! {driver} прибыл, но диспетчер не ответил "
            f"за {ARRIVED_TIMEOUT // 60} минут.{location_text}"
        )
    )

    # Уведомляем группу диспетчеров
    if DISPATCH_GROUP_ID:
        await context.bot.send_message(
            chat_id=DISPATCH_GROUP_ID,
            text=(
                f"🚨 ТРЕБУЕТСЯ ВНИМАНИЕ!\n\n"
                f"Водитель {driver} прибыл на место, но не получил "
                f"подтверждения от диспетчера в течение {ARRIVED_TIMEOUT // 60} минут.{location_text}\n\n"
                f"Пожалуйста, свяжитесь с водителем или подтвердите прибытие.",
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"✅ Подтвердить ({driver})",
                    callback_data=f"confirm_arrived_{job_name}"
                )
            ]])
        )
        log.info(f"Эскалация отправлена в группу диспетчеров {DISPATCH_GROUP_ID}")
    else:
        log.warning("DISPATCH_GROUP_ID не задан — эскалация не отправлена")


async def cb_confirm_arrived(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dispatcher подтверждает прибытие."""
    q = update.callback_query
    await q.answer("✅ Прибытие подтверждено!")

    job_name   = q.data.replace("confirm_arrived_", "")
    data       = arrived_pending.get(job_name)
    dispatcher = update.effective_user.full_name or "Dispatcher"

    if not data:
        await q.message.edit_reply_markup(reply_markup=None)
        await q.message.reply_text("ℹ️ Это прибытие уже было подтверждено ранее.")
        return

    if data["confirmed"]:
        await q.message.edit_reply_markup(reply_markup=None)
        await q.message.reply_text("ℹ️ Уже подтверждено.")
        return

    # Отмечаем как подтверждённое
    arrived_pending[job_name]["confirmed"] = True

    # Отменяем таймер эскалации
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    # Обновляем сообщение — убираем кнопку
    await q.message.edit_reply_markup(reply_markup=None)

    # Уведомляем в группе водителя
    driver_name = data["driver_name"]
    await context.bot.send_message(
        chat_id=data["chat_id"],
        text=(
            f"✅ Прибытие {driver_name} подтверждено!\n"
            f"👤 Подтвердил: {dispatcher}"
        )
    )

    # Если подтверждение пришло из группы диспетчеров — дополнительно уведомляем
    if update.effective_chat.id == DISPATCH_GROUP_ID:
        await q.message.reply_text(
            f"✅ {dispatcher} подтвердил прибытие {driver_name}."
        )

    log.info(f"Прибытие {driver_name} подтверждено диспетчером {dispatcher}")



# ══════════════════════════════════════════════════════════════
# КОМАНДА /arrived — ПРИБЫТИЕ ВОДИТЕЛЯ
# ══════════════════════════════════════════════════════════════
# Хранилище ожидающих подтверждений:
# { job_name: {"driver_name", "driver_chat_id", "group_chat_id", "message", "arrived_at"} }
arrived_pending: dict[str, dict] = {}


async def job_arrived_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывает если диспетчер не ответил за 5 минут."""
    data = context.job.data
    driver_name = data["driver_name"]
    driver_chat_id = data["driver_chat_id"]
    group_chat_id = data["group_chat_id"]
    arrived_msg = data["message"]
    arrived_at = data["arrived_at"]
    job_name = context.job.name

    # Убираем из pending
    arrived_pending.pop(job_name, None)

    log.warning(f"Arrival timeout: {driver_name} received no dispatcher response")

    # Уведомляем группу диспетчеров
    await notify_dispatchers(
        context.bot,
        f"🚨 ALERT! No response to driver!\n\n"
        f"👤 Driver: {driver_name}\n"
        f"⏰ Arrived: {arrived_at}\n"
        f"📍 Message: {arrived_msg}\n\n"
        f"⏳ 5 minutes passed — dispatcher did not respond!\n"
        f"Please contact the driver."
    )

    # Уведомляем водителя
    try:
        await context.bot.send_message(
            chat_id=group_chat_id,
            text=(
                "⏰ Dispatcher has not responded yet.\n"
                "Dispatcher group has been notified — please wait for contact."
            )
        )
    except Exception as e:
        log.warning(f"Не удалось уведомить водителя: {e}")


async def cmd_arrived(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Водитель сообщает о прибытии. Запускает таймер ожидания ответа диспетчера."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    arrived_at = fmt_time()

    # Дополнительное сообщение от водителя (если написал после команды)
    extra = " ".join(context.args) if context.args else ""
    location_text = f" — {extra}" if extra else ""

    msg_text = f"📍 Arrived{location_text}"
    arrived_at_full = fmt_datetime()

    # Отправляем уведомление в группу
    await update.message.reply_text(
        f"✅ Arrival confirmed at {arrived_at}\n\n"
        f"⏳ Waiting for dispatcher response...\n"
        f"If no response within 5 minutes — dispatcher group will be notified automatically."
    )

    # Уведомляем диспетчерскую группу сразу
    await notify_dispatchers(
        context.bot,
        f"📍 Driver has arrived!\n\n"
        f"👤 {user.full_name}\n"
        f"⏰ Time: {arrived_at_full}\n"
        f"💬 {msg_text}\n\n"
        f"⚠️ Please respond to the driver within 5 minutes."
    )

    # Запускаем таймер
    job_name = f"arrived_{user.id}_{chat_id}"
    # Отменяем предыдущий если был
    for old_job in context.job_queue.get_jobs_by_name(job_name):
        old_job.schedule_removal()

    job_data = {
        "driver_name": user.full_name,
        "driver_chat_id": user.id,
        "group_chat_id": chat_id,
        "message": msg_text,
        "arrived_at": arrived_at_full,
    }
    context.job_queue.run_once(
        job_arrived_timeout,
        when=ARRIVED_TIMEOUT_SEC,
        data=job_data,
        name=job_name,
    )
    arrived_pending[job_name] = job_data
    log.info(f"Arrival timer started: {user.full_name}, job={job_name}")


async def handle_dispatcher_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отслеживает ответы в группах водителей.
    Если оператор ответил — отменяем таймер.
    """
    msg = update.message
    if not msg:
        return

    user_id = msg.from_user.id
    chat_id = msg.chat_id

    # Проверяем только операторов
    if not is_op(user_id):
        return

    # Ищем активный таймер для этой группы
    # job_name = arrived_{driver_user_id}_{chat_id}
    jobs_to_cancel = []
    for job_name, data in list(arrived_pending.items()):
        if data["group_chat_id"] == chat_id:
            jobs_to_cancel.append(job_name)

    for job_name in jobs_to_cancel:
        for job in context.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()
        data = arrived_pending.pop(job_name, {})
        driver_name = data.get("driver_name", "Водитель")
        log.info(f"Timer cancelled: dispatcher responded to driver {driver_name}")
        await msg.reply_text(
            f"✅ Response recorded. Timer for {driver_name} stopped."
        )


# ══════════════════════════════════════════════════════════════
# БУДИЛЬНИК (/alarm)
# ══════════════════════════════════════════════════════════════
# Хранилище будильников: {job_name: {"driver_name", "chat_id", "wake_at"}}
alarms_pending: dict[str, dict] = {}

ALARM_CONV_SET = 700

async def job_alarm_ring(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Будильник срабатывает — ждём ответа водителя 2 минуты."""
    data = context.job.data
    chat_id = data["chat_id"]
    driver_name = data["driver_name"]
    job_name = context.job.name
    wake_at = data["wake_at"]

    # Отправляем сигнал будильника
    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⏰ ALARM! Time: {wake_at}\n\n"
                f"{driver_name}, are you awake?\n"
                f"Reply /awake within 2 minutes."
            )
        )
        alarms_pending[job_name] = {**data, "ring_msg_id": msg.message_id}
    except Exception as e:
        log.warning(f"Будильник {job_name}: {e}")
        return

    # Запускаем таймер ожидания ответа — 2 минуты
    context.job_queue.run_once(
        job_alarm_no_response,
        when=120,
        data={**data, "ring_job": job_name},
        name=f"alarm_wait_{data['user_id']}",
    )


async def job_alarm_no_response(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Водитель не responded to alarm — зовём диспетчера."""
    data = context.job.data
    chat_id = data["chat_id"]
    driver_name = data["driver_name"]
    ring_job = data.get("ring_job", "")
    wake_at = data["wake_at"]

    # Убираем из pending
    alarms_pending.pop(ring_job, None)

    log.warning(f"Driver {driver_name} did not respond to alarm")

    # Уведомляем группу диспетчеров
    await notify_dispatchers(
        context.bot,
        f"🚨 DRIVER NOT RESPONDING TO ALARM!\n\n"
        f"👤 Driver: {driver_name}\n"
        f"⏰ Alarm was set for: {wake_at}\n"
        f"⏳ 2 minutes passed — no response.\n\n"
        f"Please contact the driver immediately!"
    )

    # Уведомляем группу водителя
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🚨 No alarm response!\n"
                "Dispatchers have been notified and will contact you."
            )
        )
    except Exception as e:
        log.warning(f"Уведомление группы (будильник): {e}")


async def cmd_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Водитель устанавливает будильник."""
    if context.args:
        # Если время передано сразу: /alarm 14:30
        time_str = context.args[0].strip()
        return await _set_alarm(update, context, time_str)

    await update.message.reply_text(
        "⏰ Enter alarm time:\n\n"
        "Format: <code>14:30</code>\n"
        "Or minutes from now: <code>+90</code>",
        parse_mode="HTML"
    )
    return ALARM_CONV_SET


async def st_alarm_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает время и устанавливает будильник."""
    time_str = update.message.text.strip()
    return await _set_alarm(update, context, time_str)


async def _set_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE, time_str: str) -> int:
    """Общая логика установки будильника."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    now = now_local()

    # Разбираем время
    try:
        if time_str.startswith("+"):
            # Через N минут
            minutes = int(time_str[1:])
            wake_time = now + __import__("datetime").timedelta(minutes=minutes)
            wake_at = wake_time.astimezone(ZoneInfo(TRUCK_TIMEZONE)).strftime("%I:%M %p %Z")
            delay_sec = minutes * 60
        else:
            # Конкретное время HH:MM
            hh, mm = map(int, time_str.split(":"))
            from datetime import timedelta
            wake_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if wake_time <= now:
                wake_time += timedelta(days=1)  # if time already passed — set for tomorrow
            delay_sec = int((wake_time - now).total_seconds())
            wake_at = wake_time.astimezone(ZoneInfo(TRUCK_TIMEZONE)).strftime("%I:%M %p %Z")
    except Exception:
        await update.message.reply_text(
            "❌ Invalid format.\n\n"
            "Примеры:\n"
            "• <code>14:30</code> — at that time\n"
            "• <code>+90</code> — in 90 minutes",
            parse_mode="HTML"
        )
        return ALARM_CONV_SET

    # Отменяем старый будильник этого водителя в этом чате
    old_job_name = f"alarm_{user.id}_{chat_id}"
    for job in context.job_queue.get_jobs_by_name(old_job_name):
        job.schedule_removal()

    job_data = {
        "driver_name": user.full_name,
        "user_id": user.id,
        "chat_id": chat_id,
        "wake_at": wake_at,
    }
    context.job_queue.run_once(
        job_alarm_ring,
        when=delay_sec,
        data=job_data,
        name=old_job_name,
    )

    hours_left = delay_sec // 3600
    mins_left = (delay_sec % 3600) // 60

    if hours_left > 0:
        time_label = f"{hours_left}ч {mins_left}мин"
    else:
        time_label = f"{mins_left}мин"

    await update.message.reply_text(
        f"⏰ Alarm set for {wake_at}\n"
        f"Time until alarm: {time_label}\n\n"
        f"When it rings — reply /awake to confirm."
    )
    log.info(f"Alarm: {user.full_name} → {wake_at} (in {delay_sec}s)")
    return ConversationHandler.END


async def cmd_awake(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Водитель подтверждает что проснулся."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Отменяем таймер ожидания ответа
    wait_job_name = f"alarm_wait_{user.id}"
    cancelled = False
    for job in context.job_queue.get_jobs_by_name(wait_job_name):
        job.schedule_removal()
        cancelled = True

    # Убираем из pending
    alarm_job_name = f"alarm_{user.id}_{chat_id}"
    alarms_pending.pop(alarm_job_name, None)

    if cancelled:
        await update.message.reply_text(
            f"✅ {user.full_name} is awake! Safe travels! 🚛"
        )
        log.info(f"{user.full_name} responded to alarm")
    else:
        await update.message.reply_text(
            "✅ Confirmed! Safe travels! 🚛"
        )


def build_alarm_conv():
    return ConversationHandler(
        entry_points=[CommandHandler("alarm", cmd_alarm)],
        states={
            ALARM_CONV_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_alarm_time)],
        },
        fallbacks=[CommandHandler("cancel", rw_cancel)],
        per_user=True,
        per_chat=False,
        per_message=False,
        allow_reentry=True,
    )


# ── ГРУППЫ ДИСПЕТЧЕРОВ ───────────────────────────────────────
async def sec_dispatchers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список групп диспетчеров."""
    if not is_op_update(update):
        return
    groups = get_dispatcher_groups(active_only=False)
    rows = []
    for g in groups:
        rows.append([InlineKeyboardButton(
            f"📢 {g['title']} ({g['chat_id']})",
            callback_data=f"disp_del_{g['chat_id']}"
        )])
    rows.append([InlineKeyboardButton("➕ Add group", callback_data="disp_add")])

    text = "📢 Dispatcher groups:\n\n"
    if groups:
        text += "\n".join(f"• {g['title']} ({g['chat_id']})" for g in groups)
    else:
        text += "No groups yet.\nAdd группу диспетчеров чтобы бот мог отправлять туда уведомления."

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def cb_disp_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "Enter the dispatcher group chat_id.\n\n"
        "How to get the ID:\n"
        "1. Add @RawDataBot в группу диспетчеров\n"
        "2. Copy the number from the chat.id field\n"
        "3. Remove @RawDataBot from the group\n\n"
        "Also add the bot to the dispatcher group!"
    )
    return ST_DISP_CHAT


async def st_disp_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Invalid format. Enter numeric ID:")
        return ST_DISP_CHAT
    context.user_data["disp_chat_id"] = chat_id
    await update.message.reply_text("Enter group name (e.g.: Dispatchers NY):")
    return ST_DISP_TITLE


async def st_disp_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    chat_id = context.user_data.pop("disp_chat_id", 0)
    add_dispatcher_group(chat_id, title)
    await update.message.reply_text(
        f"✅ Dispatcher group added!\n\n"
        f"📢 {title}\n"
        f"ID: {chat_id}\n\n"
        "Bot will now send arrival and alarm notifications there.",
        reply_markup=kb_op()
    )
    return ConversationHandler.END


async def cb_disp_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = int(q.data.split("_")[-1])
    groups = get_dispatcher_groups(active_only=False)
    g = next((x for x in groups if x["chat_id"] == chat_id), None)
    if g:
        await q.message.reply_text(
            f"Remove group «{g['title']}»?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Delete", callback_data=f"disp_confirm_del_{chat_id}"),
                InlineKeyboardButton("◀️ Cancel", callback_data="nav_dispatchers"),
            ]])
        )


async def cb_disp_confirm_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = int(q.data.split("_")[-1])
    delete_dispatcher_group(chat_id)
    await q.message.reply_text("🗑 Группа removed.", reply_markup=kb_op())


def main():
    init_db()
    log.info("DB initialized.")

    app = Application.builder().token(BOT_TOKEN).build()

    # ── Команды ───────────────────────────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("arrived", cmd_arrived))
    app.add_handler(CommandHandler("awake", cmd_awake))
    app.add_handler(build_alarm_conv())
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("arrived", cmd_arrived))
    app.add_handler(CommandHandler("awake", cmd_awake))
    app.add_handler(build_alarm_conv())

    # ── ConversationHandler-ы (приоритет над всеми текстовыми) ─
    app.add_handler(build_weather_conv())
    app.add_handler(build_operator_conv())

    # ── Кнопки меню оператора ─────────────────────────────────
    app.add_handler(MessageHandler(filters.Regex("^👥 Drivers$"), sec_drivers))
    app.add_handler(MessageHandler(filters.Regex("^📢 Dispatchers$"), sec_dispatchers))
    app.add_handler(MessageHandler(filters.Regex("^📋 Templates$"), sec_templates))
    app.add_handler(MessageHandler(filters.Regex("^🕐 Schedules$"), sec_schedules))

    # ── Ответ диспетчера — отменяет таймер ─────────────────────
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
        handle_dispatcher_reply,
    ))

    # ── Геолокация ────────────────────────────────────────────
    app.add_handler(MessageHandler(filters.LOCATION, handle_live_location))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.LOCATION, handle_live_location))

    # ── Inline callbacks ──────────────────────────────────────
    app.add_handler(CallbackQueryHandler(cb_autotrip, pattern=r"^autotrip_"))
    app.add_handler(CallbackQueryHandler(cb_confirm_arrived, pattern=r"^confirm_arrived_"))
    app.add_handler(CallbackQueryHandler(cb_drv_edit,   pattern=r"^drv_edit_-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_drv_toggle, pattern=r"^drv_toggle_-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_drv_del,    pattern=r"^drv_del_-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_tpl_view,   pattern=r"^tpl_view_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_tpl_del,    pattern=r"^tpl_del_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_sch_view,   pattern=r"^sch_view_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_sch_toggle, pattern=r"^sch_toggle_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_sch_del,    pattern=r"^sch_del_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_disp_del,         pattern=r"^disp_del_-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_disp_confirm_del, pattern=r"^disp_confirm_del_-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_nav, pattern=r"^(back_main|nav_drivers|nav_templates|nav_schedules|nav_dispatchers)$"))

    # ── Автодетект Trip ID — ПОСЛЕДНИМ ────────────────────────
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.FORWARDED) & ~filters.COMMAND,
        auto_detect_trip
    ))

    async def on_start(app):
        register_all_schedules(app)
        log.info("Schedules loaded.")

        # Устанавливаем команды для групп
        from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
        group_commands = [
            BotCommand("liveweather", "🌤 Weather / route / live tracking"),
            BotCommand("arrived",     "📍 Notify dispatcher of arrival"),
            BotCommand("alarm",       "⏰ Set wake-up alarm"),
            BotCommand("awake",       "✅ Confirm I'm awake"),
            BotCommand("cancel",      "❌ Cancel current action"),
        ]
        private_commands = [
            BotCommand("start",       "👨‍💼 Operator panel"),
            BotCommand("liveweather", "🌤 Weather / route / live tracking"),
            BotCommand("myid",        "🔑 My Telegram ID"),
            BotCommand("chatid",      "📢 Get this chat ID"),
            BotCommand("cancel",      "❌ Cancel current action"),
        ]
        # Команды для операторских групп
        operator_group_commands = [
            BotCommand("start",    "👨‍💼 Operator panel"),
            BotCommand("liveweather", "🌤 Weather / route / tracking"),
            BotCommand("myid",     "🔑 My Telegram ID"),
            BotCommand("chatid",   "📢 This chat ID"),
            BotCommand("cancel",   "❌ Cancel action"),
        ]
        try:
            await app.bot.set_my_commands(group_commands,   scope=BotCommandScopeAllGroupChats())
            await app.bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
            # Устанавливаем расширенные команды для группы диспетчеров
            from telegram import BotCommandScopeChat
            for op_chat_id in OPERATOR_CHAT_IDS:
                try:
                    await app.bot.set_my_commands(
                        operator_group_commands,
                        scope=BotCommandScopeChat(chat_id=op_chat_id)
                    )
                    log.info(f"Operator commands set for chat {op_chat_id}")
                except Exception as e2:
                    log.warning(f"Команды для чата {op_chat_id}: {e2}")
            log.info("Bot commands set.")
        except Exception as e:
            log.warning(f"Failed to set commands: {e}")

    app.post_init = on_start

    log.info(f"Bot started. TEST_MODE={TEST_MODE}, TZ={TRUCK_TIMEZONE}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
