"""
handlers/operator.py — панель оператора
Управление водителями, шаблонами, расписаниями и рассылками.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters,
)
from handlers.common import is_operator, kb_operator_main, kb_back
from db import database as db
from scheduler.jobs import register_schedule, unregister_schedule

log = logging.getLogger(__name__)

# ── ConversationHandler states ────────────────────────────────
(
    # Водители
    ST_DRIVER_NAME, ST_DRIVER_CHAT, ST_DRIVER_EDIT_NAME,
    # Шаблоны
    ST_TPL_TITLE, ST_TPL_TEXT, ST_TPL_EDIT_TITLE, ST_TPL_EDIT_TEXT,
    # Расписания
    ST_SCH_TITLE, ST_SCH_TEXT, ST_SCH_CRON, ST_SCH_TARGET,
    # Рассылка
    ST_BROADCAST_TEXT, ST_BROADCAST_TARGET,
) = range(13)


# ════════════════════════════════════════════════════════════════
# ГЛАВНОЕ МЕНЮ ОПЕРАТОРА
# ════════════════════════════════════════════════════════════════
async def cmd_operator(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_operator(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await update.message.reply_text(
        "👨‍💼 Панель оператора\nВыберите раздел:",
        reply_markup=kb_operator_main(),
    )


# ════════════════════════════════════════════════════════════════
# РАЗДЕЛ: ВОДИТЕЛИ
# ════════════════════════════════════════════════════════════════
async def section_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_operator(update.effective_user.id):
        return
    drivers = db.get_all_drivers(active_only=False)
    text = "👥 Список водителей:\n\n"
    buttons = []

    if drivers:
        for d in drivers:
            status = "✅" if d["active"] else "❌"
            text += f"{status} {d['name']} — чат {d['chat_id']}\n"
            buttons.append([
                InlineKeyboardButton(
                    f"✏️ {d['name']}", callback_data=f"driver_edit_{d['chat_id']}"
                )
            ])
    else:
        text += "Пока нет водителей."

    buttons.append([InlineKeyboardButton("➕ Добавить водителя", callback_data="driver_add")])
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_driver_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "Введите имя водителя:",
        reply_markup=kb_back("drivers_list"),
    )
    return ST_DRIVER_NAME


async def st_driver_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_driver_name"] = update.message.text.strip()
    await update.message.reply_text(
        "Теперь введите ID группы (chat_id) водителя.\n\n"
        "Как узнать: добавьте @userinfobot в группу и напишите /start"
    )
    return ST_DRIVER_CHAT


async def st_driver_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        chat_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Неверный формат. Введите числовой ID группы:")
        return ST_DRIVER_CHAT

    name = context.user_data.pop("new_driver_name", "Водитель")
    if db.add_driver(chat_id, name):
        await update.message.reply_text(
            f"✅ Водитель {name} добавлен (чат {chat_id}).",
            reply_markup=kb_operator_main(),
        )
    else:
        await update.message.reply_text(
            f"⚠️ Водитель с chat_id {chat_id} уже существует.",
            reply_markup=kb_operator_main(),
        )
    return ConversationHandler.END


async def cb_driver_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[-1])
    driver = db.get_driver(chat_id)
    if not driver:
        await query.message.reply_text("Водитель не найден.")
        return ConversationHandler.END

    context.user_data["edit_driver_chat_id"] = chat_id
    active_label = "Деактивировать" if driver["active"] else "Активировать"
    await query.message.reply_text(
        f"Водитель: {driver['name']}\nЧат: {chat_id}\n\nДействие:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить имя", callback_data=f"driver_rename_{chat_id}")],
            [InlineKeyboardButton(f"🔄 {active_label}", callback_data=f"driver_toggle_{chat_id}")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"driver_delete_{chat_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="drivers_list")],
        ])
    )
    return ConversationHandler.END


async def cb_driver_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[-1])
    driver = db.get_driver(chat_id)
    if driver:
        db.toggle_driver(chat_id, not driver["active"])
        status = "активирован ✅" if not driver["active"] else "деактивирован ❌"
        await query.message.reply_text(f"Водитель {driver['name']} {status}.")


async def cb_driver_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split("_")[-1])
    driver = db.get_driver(chat_id)
    if driver:
        db.delete_driver(chat_id)
        await query.message.reply_text(f"🗑 Водитель {driver['name']} удалён.")


# ════════════════════════════════════════════════════════════════
# РАЗДЕЛ: ШАБЛОНЫ
# ════════════════════════════════════════════════════════════════
async def section_templates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_operator(update.effective_user.id):
        return
    templates = db.get_templates()
    buttons = []
    for t in templates:
        buttons.append([InlineKeyboardButton(t["title"], callback_data=f"tpl_view_{t['id']}")])
    buttons.append([InlineKeyboardButton("➕ Новый шаблон", callback_data="tpl_add")])
    await update.message.reply_text(
        "📋 Шаблоны сообщений:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_tpl_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tpl_id = int(query.data.split("_")[-1])
    tpl = db.get_template(tpl_id)
    if not tpl:
        await query.message.reply_text("Шаблон не найден.")
        return
    await query.message.reply_text(
        f"📋 {tpl['title']}\n\n{tpl['text']}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📨 Отправить", callback_data=f"tpl_send_{tpl_id}")],
            [InlineKeyboardButton("✏️ Редактировать", callback_data=f"tpl_edit_{tpl_id}")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"tpl_delete_{tpl_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="templates_list")],
        ])
    )


async def cb_tpl_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Введите название шаблона:")
    return ST_TPL_TITLE


async def st_tpl_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["tpl_title"] = update.message.text.strip()
    await update.message.reply_text("Теперь введите текст шаблона:")
    return ST_TPL_TEXT


async def st_tpl_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = context.user_data.pop("tpl_title", "")
    text = update.message.text.strip()
    db.add_template(title, text)
    await update.message.reply_text(
        f"✅ Шаблон «{title}» сохранён.",
        reply_markup=kb_operator_main(),
    )
    return ConversationHandler.END


async def cb_tpl_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tpl_id = int(query.data.split("_")[-1])
    tpl = db.get_template(tpl_id)
    if tpl:
        db.delete_template(tpl_id)
        await query.message.reply_text(f"🗑 Шаблон «{tpl['title']}» удалён.")


async def cb_tpl_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Быстрая отправка шаблона — переходим в рассылку с готовым текстом."""
    query = update.callback_query
    await query.answer()
    tpl_id = int(query.data.split("_")[-1])
    tpl = db.get_template(tpl_id)
    if not tpl:
        await query.message.reply_text("Шаблон не найден.")
        return ConversationHandler.END

    context.user_data["broadcast_text"] = tpl["text"]
    await query.message.reply_text(
        f"Шаблон: «{tpl['title']}»\n\nКому отправить?",
        reply_markup=_broadcast_target_kb(),
    )
    return ST_BROADCAST_TARGET


# ════════════════════════════════════════════════════════════════
# РАЗДЕЛ: РАСПИСАНИЯ
# ════════════════════════════════════════════════════════════════
async def section_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_operator(update.effective_user.id):
        return
    schedules = db.get_schedules()
    buttons = []
    for s in schedules:
        icon = "✅" if s["active"] else "⏸"
        buttons.append([InlineKeyboardButton(
            f"{icon} {s['title']} ({s['cron_expr']})",
            callback_data=f"sch_view_{s['id']}"
        )])
    buttons.append([InlineKeyboardButton("➕ Новое расписание", callback_data="sch_add")])
    await update.message.reply_text(
        "🕐 Расписания уведомлений:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_sch_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sched_id = int(query.data.split("_")[-1])
    s = db.get_schedule(sched_id)
    if not s:
        await query.message.reply_text("Расписание не найдено.")
        return

    target_label = "Все водители" if s["target"] == "all" else s["target"]
    status = "✅ Активно" if s["active"] else "⏸ Приостановлено"
    await query.message.reply_text(
        f"🕐 {s['title']}\n"
        f"Расписание: {s['cron_expr']}\n"
        f"Получатели: {target_label}\n"
        f"Статус: {status}\n\n"
        f"Текст:\n{s['text']}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "⏸ Приостановить" if s["active"] else "▶️ Возобновить",
                callback_data=f"sch_toggle_{sched_id}"
            )],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"sch_delete_{sched_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="schedules_list")],
        ])
    )


async def cb_sch_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Введите название расписания (например: PTI утром):")
    return ST_SCH_TITLE


async def st_sch_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["sch_title"] = update.message.text.strip()
    await update.message.reply_text("Введите текст уведомления:")
    return ST_SCH_TEXT


async def st_sch_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["sch_text"] = update.message.text.strip()
    await update.message.reply_text(
        "Введите расписание в одном из форматов:\n\n"
        "• <code>09:00</code> — каждый день в 09:00\n"
        "• <code>08:00|mon,wed,fri</code> — в пн, ср, пт\n"
        "• <code>09:00|1</code> — 1-го числа (первая неделя месяца)\n"
        "• <code>*/4h</code> — каждые 4 часа",
        parse_mode="HTML",
    )
    return ST_SCH_CRON


async def st_sch_cron(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["sch_cron"] = update.message.text.strip()
    await update.message.reply_text(
        "Кому отправлять?",
        reply_markup=_broadcast_target_kb(),
    )
    return ST_SCH_TARGET


async def st_sch_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    target = query.data.replace("target_", "")
    if target == "all":
        target_value = "all"
    else:
        # конкретный водитель — chat_id из callback
        target_value = target

    title = context.user_data.pop("sch_title", "")
    text = context.user_data.pop("sch_text", "")
    cron = context.user_data.pop("sch_cron", "09:00")

    sched_id = db.add_schedule(title, text, cron, target_value)

    # Регистрируем джоб
    from telegram.ext import Application
    sched = db.get_schedule(sched_id)
    register_schedule(context.application, dict(sched))

    await query.message.reply_text(
        f"✅ Расписание «{title}» создано.\nВремя: {cron}",
        reply_markup=kb_operator_main(),
    )
    return ConversationHandler.END


async def cb_sch_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sched_id = int(query.data.split("_")[-1])
    s = db.get_schedule(sched_id)
    if not s:
        return
    new_active = 0 if s["active"] else 1
    db.update_schedule(sched_id, active=new_active)

    if new_active:
        register_schedule(context.application, dict(db.get_schedule(sched_id)))
        await query.message.reply_text(f"▶️ Расписание «{s['title']}» возобновлено.")
    else:
        unregister_schedule(context.application, sched_id)
        await query.message.reply_text(f"⏸ Расписание «{s['title']}» приостановлено.")


async def cb_sch_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sched_id = int(query.data.split("_")[-1])
    s = db.get_schedule(sched_id)
    if s:
        unregister_schedule(context.application, sched_id)
        db.delete_schedule(sched_id)
        await query.message.reply_text(f"🗑 Расписание «{s['title']}» удалено.")


# ════════════════════════════════════════════════════════════════
# РАЗДЕЛ: РАССЫЛКА
# ════════════════════════════════════════════════════════════════
def _broadcast_target_kb() -> InlineKeyboardMarkup:
    drivers = db.get_all_drivers(active_only=True)
    buttons = [[InlineKeyboardButton("📢 Всем водителям", callback_data="target_all")]]
    for d in drivers:
        buttons.append([InlineKeyboardButton(
            f"👤 {d['name']}", callback_data=f"target_{d['chat_id']}"
        )])
    return InlineKeyboardMarkup(buttons)


async def section_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_operator(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text(
        "📨 Рассылка\n\nВведите текст сообщения (или отправьте фото/файл с подписью):",
        reply_markup=kb_back("back_main"),
    )
    return ST_BROADCAST_TEXT


async def st_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Поддержка текста и фото
    if update.message.photo:
        context.user_data["broadcast_photo"] = update.message.photo[-1].file_id
        context.user_data["broadcast_text"] = update.message.caption or ""
    elif update.message.document:
        context.user_data["broadcast_doc"] = update.message.document.file_id
        context.user_data["broadcast_text"] = update.message.caption or ""
    else:
        context.user_data["broadcast_text"] = update.message.text.strip()

    await update.message.reply_text(
        "Кому отправить?",
        reply_markup=_broadcast_target_kb(),
    )
    return ST_BROADCAST_TARGET


async def st_broadcast_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    target = query.data.replace("target_", "")
    text = context.user_data.pop("broadcast_text", "")
    photo = context.user_data.pop("broadcast_photo", None)
    doc = context.user_data.pop("broadcast_doc", None)

    if target == "all":
        drivers = db.get_all_drivers(active_only=True)
        chat_ids = [d["chat_id"] for d in drivers]
    else:
        chat_ids = [int(target)]

    sent = 0
    for chat_id in chat_ids:
        try:
            if photo:
                await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=text)
            elif doc:
                await context.bot.send_document(chat_id=chat_id, document=doc, caption=text)
            else:
                await context.bot.send_message(chat_id=chat_id, text=text)
            db.log_send(chat_id, text, source="manual")
            sent += 1
        except Exception as e:
            log.warning(f"Рассылка: ошибка для {chat_id}: {e}")

    await query.message.reply_text(
        f"✅ Отправлено: {sent} из {len(chat_ids)} водителей.",
        reply_markup=kb_operator_main(),
    )
    return ConversationHandler.END


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=kb_operator_main())
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════
# Callback роутер для навигации
# ════════════════════════════════════════════════════════════════
async def cb_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back_main":
        await query.message.reply_text("Главное меню:", reply_markup=kb_operator_main())
    elif data == "drivers_list":
        drivers = db.get_all_drivers(active_only=False)
        buttons = [
            [InlineKeyboardButton(
                ("✅ " if d["active"] else "❌ ") + d["name"],
                callback_data=f"driver_edit_{d['chat_id']}"
            )]
            for d in drivers
        ]
        buttons.append([InlineKeyboardButton("➕ Добавить водителя", callback_data="driver_add")])
        await query.message.reply_text("👥 Водители:", reply_markup=InlineKeyboardMarkup(buttons))
    elif data == "templates_list":
        templates = db.get_templates()
        buttons = [[InlineKeyboardButton(t["title"], callback_data=f"tpl_view_{t['id']}")] for t in templates]
        buttons.append([InlineKeyboardButton("➕ Новый шаблон", callback_data="tpl_add")])
        await query.message.reply_text("📋 Шаблоны:", reply_markup=InlineKeyboardMarkup(buttons))
    elif data == "schedules_list":
        schedules = db.get_schedules()
        buttons = [
            [InlineKeyboardButton(
                ("✅ " if s["active"] else "⏸ ") + s["title"],
                callback_data=f"sch_view_{s['id']}"
            )]
            for s in schedules
        ]
        buttons.append([InlineKeyboardButton("➕ Новое расписание", callback_data="sch_add")])
        await query.message.reply_text("🕐 Расписания:", reply_markup=InlineKeyboardMarkup(buttons))


# ════════════════════════════════════════════════════════════════
# Сборка ConversationHandler
# ════════════════════════════════════════════════════════════════
def build_operator_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            # Добавление водителя
            CallbackQueryHandler(cb_driver_add, pattern="^driver_add$"),
            # Шаблоны
            CallbackQueryHandler(cb_tpl_add, pattern="^tpl_add$"),
            CallbackQueryHandler(cb_tpl_send, pattern=r"^tpl_send_\d+$"),
            # Расписания
            CallbackQueryHandler(cb_sch_add, pattern="^sch_add$"),
            # Рассылка
            MessageHandler(filters.Regex("^📨 Рассылка$"), section_broadcast),
        ],
        states={
            ST_DRIVER_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, st_driver_name)],
            ST_DRIVER_CHAT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, st_driver_chat)],
            ST_TPL_TITLE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, st_tpl_title)],
            ST_TPL_TEXT:        [MessageHandler(filters.TEXT & ~filters.COMMAND, st_tpl_text)],
            ST_SCH_TITLE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, st_sch_title)],
            ST_SCH_TEXT:        [MessageHandler(filters.TEXT & ~filters.COMMAND, st_sch_text)],
            ST_SCH_CRON:        [MessageHandler(filters.TEXT & ~filters.COMMAND, st_sch_cron)],
            ST_SCH_TARGET:      [CallbackQueryHandler(st_sch_target, pattern=r"^target_")],
            ST_BROADCAST_TEXT:  [MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
                st_broadcast_text
            )],
            ST_BROADCAST_TARGET: [CallbackQueryHandler(st_broadcast_target, pattern=r"^target_")],
        },
        fallbacks=[CommandHandler("cancel", conv_cancel)],
        per_chat=False,
        per_user=True,
    )
