"""
bot.py — Trucking Bot, точка входа
python-telegram-bot v21
"""
import logging
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters,
)

from config import BOT_TOKEN
from db.database import init_db
from scheduler.jobs import register_all_schedules
from handlers.common import is_operator, kb_operator_main
from handlers.operator import (
    cmd_operator, section_drivers, section_templates,
    section_schedules, cb_navigation, build_operator_conv,
    cb_driver_edit, cb_driver_toggle, cb_driver_delete,
    cb_tpl_view, cb_tpl_delete,
    cb_sch_view, cb_sch_toggle, cb_sch_delete,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


async def cmd_start(update, context):
    user_id = update.effective_user.id
    if is_operator(user_id):
        await update.message.reply_text(
            "👨‍💼 Добро пожаловать, оператор!",
            reply_markup=kb_operator_main(),
        )
    else:
        await update.message.reply_text(
            "🚛 Trucking Bot активен.\nОжидайте уведомлений от диспетчера."
        )


def main() -> None:
    # Инициализация БД
    init_db()
    log.info("БД инициализирована.")

    app = Application.builder().token(BOT_TOKEN).build()

    # ── Глобальные команды ────────────────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("op", cmd_operator))

    # ── ConversationHandler (диалоги добавления/рассылки) ─────
    app.add_handler(build_operator_conv())

    # ── Кнопки главного меню ──────────────────────────────────
    app.add_handler(MessageHandler(filters.Regex("^👥 Водители$"), section_drivers))
    app.add_handler(MessageHandler(filters.Regex("^📋 Шаблоны$"), section_templates))
    app.add_handler(MessageHandler(filters.Regex("^🕐 Расписания$"), section_schedules))

    # ── Callback роутер ───────────────────────────────────────
    app.add_handler(CallbackQueryHandler(cb_driver_edit,   pattern=r"^driver_edit_-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_driver_toggle, pattern=r"^driver_toggle_-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_driver_delete, pattern=r"^driver_delete_-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_tpl_view,      pattern=r"^tpl_view_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_tpl_delete,    pattern=r"^tpl_delete_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_sch_view,      pattern=r"^sch_view_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_sch_toggle,    pattern=r"^sch_toggle_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_sch_delete,    pattern=r"^sch_delete_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_navigation,    pattern=r"^(back_main|drivers_list|templates_list|schedules_list)$"))

    # ── Загрузка расписаний из БД ─────────────────────────────
    async def on_startup(app):
        register_all_schedules(app)
        log.info("Расписания загружены.")

    app.post_init = on_startup

    log.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
