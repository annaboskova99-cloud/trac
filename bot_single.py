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
            text       TEXT NOT NULL,
            cron_expr  TEXT NOT NULL,
            target     TEXT NOT NULL,
            active     INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS send_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id  INTEGER NOT NULL,
            text     TEXT,
            sent_at  TEXT DEFAULT (datetime('now')),
            source   TEXT
        );
        """)
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

def add_schedule(title, text, cron_expr, target):
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO schedules (title,text,cron_expr,target) VALUES (?,?,?,?)",
            (title, text, cron_expr, target)
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
    for cid in chat_ids:
        try:
            await context.bot.send_message(chat_id=cid, text=s["text"])
            log_send(cid, s["text"], "schedule")
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
            ST_SCH_TEXT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, st_sch_text)],
            ST_SCH_CRON:  [MessageHandler(filters.TEXT & ~filters.COMMAND, st_sch_cron)],
            ST_SCH_TARGET:[CallbackQueryHandler(st_sch_target, pattern=r"^target_")],
            ST_BC_TEXT:   [MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, st_bc_text)],
            ST_BC_TARGET: [CallbackQueryHandler(st_bc_target, pattern=r"^target_")],
        },
        fallbacks=[CommandHandler("cancel", conv_cancel)],
        per_user=True, per_chat=False,
    )


def main():
    init_db()
    log.info("БД инициализирована.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(build_conv())

    app.add_handler(MessageHandler(filters.Regex("^👥 Водители$"),   sec_drivers))
    app.add_handler(MessageHandler(filters.Regex("^📋 Шаблоны$"),    sec_templates))
    app.add_handler(MessageHandler(filters.Regex("^🕐 Расписания$"), sec_schedules))

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
