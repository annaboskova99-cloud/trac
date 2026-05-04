"""
scheduler/jobs.py — управление расписаниями уведомлений
"""
import logging
from datetime import time as dtime

from telegram.ext import Application

from db.database import get_schedules, get_all_drivers, log_send

log = logging.getLogger(__name__)


def parse_cron(cron_expr: str) -> dict:
    """
    Парсит строку расписания.
    Форматы:
      "09:00"               → каждый день в 09:00
      "09:00|mon,wed,fri"   → в указанные дни недели
      "09:00|1"             → 1-го числа каждого месяца (для инспекций)
      "*/4h"                → каждые 4 часа
    """
    parts = cron_expr.strip().split("|")
    time_part = parts[0].strip()
    extra = parts[1].strip() if len(parts) > 1 else None

    result = {}

    if time_part.startswith("*/") and time_part.endswith("h"):
        # интервал в часах: */4h
        hours = int(time_part[2:-1])
        result["type"] = "interval"
        result["hours"] = hours
    else:
        hh, mm = map(int, time_part.split(":"))
        result["type"] = "daily"
        result["time"] = dtime(hour=hh, minute=mm)

        if extra:
            # Дни недели: mon,tue,wed,thu,fri,sat,sun
            weekdays = {"mon": 0, "tue": 1, "wed": 2, "thu": 3,
                        "fri": 4, "sat": 5, "sun": 6}
            if any(d in extra for d in weekdays):
                result["days"] = [weekdays[d] for d in extra.split(",") if d in weekdays]
            # День месяца: число
            elif extra.isdigit():
                result["month_day"] = int(extra)

    return result


async def job_send_scheduled(context) -> None:
    """Джоб: отправляет уведомление по расписанию."""
    sched_id: int = context.job.data["sched_id"]
    from db.database import get_schedule
    sched = get_schedule(sched_id)
    if not sched or not sched["active"]:
        return

    # Определяем получателей
    if sched["target"] == "all":
        drivers = get_all_drivers(active_only=True)
        chat_ids = [d["chat_id"] for d in drivers]
    else:
        chat_ids = [int(x) for x in sched["target"].split(",") if x.strip()]

    # Фильтр по дню месяца (для инспекций)
    cron = parse_cron(sched["cron_expr"])
    if "month_day" in cron:
        from datetime import datetime
        today = datetime.now().day
        if today > 7:  # первая неделя месяца
            return

    text = sched["text"]
    for chat_id in chat_ids:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
            log_send(chat_id, text, source="schedule")
            log.info(f"Расписание #{sched_id} → чат {chat_id}")
        except Exception as e:
            log.warning(f"Не удалось отправить в чат {chat_id}: {e}")


def register_all_schedules(app: Application) -> None:
    """Загружает все активные расписания из БД и регистрирует джобы."""
    schedules = get_schedules(active_only=True)
    for sched in schedules:
        register_schedule(app, dict(sched))
    log.info(f"Зарегистрировано расписаний: {len(schedules)}")


def register_schedule(app: Application, sched: dict) -> None:
    """Регистрирует один джоб по расписанию."""
    cron = parse_cron(sched["cron_expr"])
    job_name = f"sched_{sched['id']}"
    data = {"sched_id": sched["id"]}

    # Удаляем старый джоб если есть
    unregister_schedule(app, sched["id"])

    if cron["type"] == "interval":
        app.job_queue.run_repeating(
            job_send_scheduled,
            interval=cron["hours"] * 3600,
            first=cron["hours"] * 3600,
            data=data,
            name=job_name,
        )
    else:
        days = cron.get("days")  # None = каждый день
        app.job_queue.run_daily(
            job_send_scheduled,
            time=cron["time"],
            days=tuple(days) if days else tuple(range(7)),
            data=data,
            name=job_name,
        )
    log.info(f"Джоб зарегистрирован: {job_name} ({sched['cron_expr']})")


def unregister_schedule(app: Application, sched_id: int) -> None:
    """Удаляет джоб расписания."""
    job_name = f"sched_{sched_id}"
    for job in app.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
