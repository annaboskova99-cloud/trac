"""
config.py — настройки Trucking Bot
Заполните перед запуском.
"""
import os

# ── Обязательно ──────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Telegram user_id операторов (список — можно несколько)
# Как узнать свой ID: напишите @userinfobot
OPERATOR_IDS: list[int] = [
    int(x) for x in os.getenv("OPERATOR_IDS", "123456789").split(",")
]

# ── База данных ───────────────────────────────────────────────
DB_PATH: str = os.getenv("DB_PATH", "trucking.db")

# ── Тест-режим ────────────────────────────────────────────────
# True  → интервалы в секундах (разработка)
# False → интервалы в часах (продакшн)
TEST_MODE: bool = os.getenv("TEST_MODE", "true").lower() == "true"
