"""
db/database.py — SQLite: схема и хелперы
"""
import sqlite3
from contextlib import contextmanager
from config import DB_PATH


# ── Контекстный менеджер ─────────────────────────────────────
@contextmanager
def get_conn():
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


# ── Создание таблиц ──────────────────────────────────────────
def init_db():
    with get_conn() as conn:
        conn.executescript("""
        -- Водители / группы
        CREATE TABLE IF NOT EXISTS drivers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER UNIQUE NOT NULL,   -- ID группы Telegram
            name        TEXT NOT NULL,
            phone       TEXT,
            active      INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        -- Шаблоны сообщений
        CREATE TABLE IF NOT EXISTS templates (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            title   TEXT NOT NULL,
            text    TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- Расписания уведомлений
        CREATE TABLE IF NOT EXISTS schedules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            text        TEXT NOT NULL,
            cron_expr   TEXT NOT NULL,   -- формат: "HH:MM" или "HH:MM|mon,tue,..."
            target      TEXT NOT NULL,   -- "all" | "chat_id1,chat_id2"
            active      INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        -- Лог отправок
        CREATE TABLE IF NOT EXISTS send_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            text        TEXT,
            sent_at     TEXT DEFAULT (datetime('now')),
            source      TEXT   -- "schedule", "manual", "template"
        );
        """)

        # Дефолтные шаблоны
        existing = conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
        if existing == 0:
            conn.executemany(
                "INSERT INTO templates (title, text) VALUES (?, ?)",
                [
                    ("PTI напоминание",
                     "📋 Напоминание: выполните Pre-Trip Inspection перед выездом.\n\n"
                     "Проверьте документы, шины, тормоза, фары и прицеп.\n"
                     "Safe truck = Safe driver ✅"),
                    ("Давление в колёсах",
                     "🛞 Проверьте давление в шинах:\n"
                     "• Передние (steer): 110–120 PSI\n"
                     "• Задние (drive): 95–105 PSI\n\n"
                     "Следуйте спецификации производителя."),
                    ("DOT Inspection Week",
                     "🚨 DOT Inspection Week / Blitz!\n\n"
                     "Убедитесь, что все документы в порядке:\n"
                     "CDL, Medical Card, Registration, Insurance, ELD.\n"
                     "Грузовик должен быть в идеальном техническом состоянии."),
                    ("Техника безопасности",
                     "⚠️ Напоминание о безопасности:\n\n"
                     "• Пристегните ремень безопасности\n"
                     "• Соблюдайте скоростной режим\n"
                     "• Делайте перерывы каждые 4 часа\n"
                     "• При усталости — остановитесь и отдохните"),
                ]
            )


# ── CRUD: водители ────────────────────────────────────────────
def add_driver(chat_id: int, name: str, phone: str = "") -> bool:
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO drivers (chat_id, name, phone) VALUES (?, ?, ?)",
                (chat_id, name, phone)
            )
            return True
        except sqlite3.IntegrityError:
            return False  # уже существует


def get_driver(chat_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM drivers WHERE chat_id = ?", (chat_id,)
        ).fetchone()


def get_all_drivers(active_only: bool = True) -> list[sqlite3.Row]:
    with get_conn() as conn:
        q = "SELECT * FROM drivers"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY name"
        return conn.execute(q).fetchall()


def update_driver(chat_id: int, name: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE drivers SET name = ? WHERE chat_id = ?", (name, chat_id))


def toggle_driver(chat_id: int, active: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE drivers SET active = ? WHERE chat_id = ?",
            (1 if active else 0, chat_id)
        )


def delete_driver(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM drivers WHERE chat_id = ?", (chat_id,))


# ── CRUD: шаблоны ─────────────────────────────────────────────
def get_templates() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM templates ORDER BY title").fetchall()


def get_template(tpl_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM templates WHERE id = ?", (tpl_id,)).fetchone()


def add_template(title: str, text: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO templates (title, text) VALUES (?, ?)", (title, text)
        )
        return cur.lastrowid


def update_template(tpl_id: int, title: str, text: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE templates SET title = ?, text = ? WHERE id = ?",
            (title, text, tpl_id)
        )


def delete_template(tpl_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM templates WHERE id = ?", (tpl_id,))


# ── CRUD: расписания ──────────────────────────────────────────
def get_schedules(active_only: bool = False) -> list[sqlite3.Row]:
    with get_conn() as conn:
        q = "SELECT * FROM schedules"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY title"
        return conn.execute(q).fetchall()


def get_schedule(sched_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM schedules WHERE id = ?", (sched_id,)
        ).fetchone()


def add_schedule(title: str, text: str, cron_expr: str, target: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO schedules (title, text, cron_expr, target) VALUES (?, ?, ?, ?)",
            (title, text, cron_expr, target)
        )
        return cur.lastrowid


def update_schedule(sched_id: int, **kwargs) -> None:
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [sched_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE schedules SET {fields} WHERE id = ?", values)


def delete_schedule(sched_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM schedules WHERE id = ?", (sched_id,))


# ── Лог ──────────────────────────────────────────────────────
def log_send(chat_id: int, text: str, source: str = "manual") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO send_log (chat_id, text, source) VALUES (?, ?, ?)",
            (chat_id, text[:500], source)
        )
