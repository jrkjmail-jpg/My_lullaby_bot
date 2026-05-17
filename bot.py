import os
import re
import uuid
import json
import sqlite3
import asyncio
import tempfile
import requests
import threading
import shutil
import time
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from dotenv import load_dotenv
from openai import OpenAI

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    PicklePersistence,
    filters,
)

load_dotenv()

BRAND_NAME = "Колыбелка"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUNO_API_KEY = os.getenv("SUNO_API_KEY")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "https://t.me/")
YOOKASSA_VAT_CODE = int(os.getenv("YOOKASSA_VAT_CODE", "1"))
YOOKASSA_TAX_SYSTEM_CODE = os.getenv("YOOKASSA_TAX_SYSTEM_CODE")
YOOKASSA_WEBHOOK_HOST = os.getenv("YOOKASSA_WEBHOOK_HOST", "0.0.0.0")
YOOKASSA_WEBHOOK_PORT = int(os.getenv("YOOKASSA_WEBHOOK_PORT", "8080"))
YOOKASSA_WEBHOOK_PATH = os.getenv("YOOKASSA_WEBHOOK_PATH", "/yookassa-webhook")
YOOKASSA_TEST_MODE = os.getenv("YOOKASSA_TEST_MODE", "").lower() in ("1", "true", "yes", "да")
ADMIN_IDS = {
    int(user_id.strip())
    for user_id in os.getenv("ADMIN_IDS", "").split(",")
    if user_id.strip().isdigit()
}

OPENAI_CLIENT = None
TELEGRAM_EVENT_LOOP = None
REMINDER_TASK = None
SUNO_BASE_URL = "https://api.sunoapi.org"
YOOKASSA_BASE_URL = "https://api.yookassa.ru/v3"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_default_db_path():
    shared_dir = os.getenv("SHARED_DIR")

    if shared_dir:
        return os.path.join(shared_dir, "kolybelka.db")

    bot_host_shared_dir = "/app/shared"

    if os.path.isdir(bot_host_shared_dir) or os.path.isdir("/app"):
        return os.path.join(bot_host_shared_dir, "kolybelka.db")

    return os.path.join(BASE_DIR, "kolybelka.db")


DB_PATH = os.getenv("DB_PATH", get_default_db_path())
PERSISTENCE_PATH = os.getenv(
    "PERSISTENCE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "kolybelka_state.pkl")
)
TEXT_GENERATION_TIMEOUT_SECONDS = int(os.getenv("TEXT_GENERATION_TIMEOUT_SECONDS", "300"))
MUSIC_GENERATION_TIMEOUT_SECONDS = int(os.getenv("MUSIC_GENERATION_TIMEOUT_SECONDS", "900"))
REMINDERS_ENABLED = os.getenv("REMINDERS_ENABLED", "1").lower() in ("1", "true", "yes", "да")
REMINDER_AFTER_DAYS = int(os.getenv("REMINDER_AFTER_DAYS", "14"))
REMINDER_INTERVAL_HOURS = int(os.getenv("REMINDER_INTERVAL_HOURS", "24"))


def is_path_inside(path, parent):
    try:
        return os.path.commonpath([
            os.path.abspath(path),
            os.path.abspath(parent),
        ]) == os.path.abspath(parent)
    except ValueError:
        return False


def is_db_path_configured_explicitly():
    return bool(os.getenv("DB_PATH") or os.getenv("SHARED_DIR"))


def is_db_path_in_project_dir():
    return is_path_inside(DB_PATH, BASE_DIR)


def is_db_path_in_shared_dir():
    return is_path_inside(DB_PATH, "/app/shared")


def get_db_persistence_warning():
    if is_db_path_in_shared_dir():
        if is_db_path_configured_explicitly():
            return (
                "✅ База лежит в /app/shared и путь задан явно. "
                "Теперь главное проверить, что общее хранилище BotHost переживает обновление с Git."
            )

        return (
            "⚠️ База выбрана в /app/shared автоматически. На BotHost нужно включить "
            "общее хранилище или явно задать DB_PATH=/app/shared/kolybelka.db. "
            "Если общее хранилище не включено, /app/shared тоже может быть временной папкой."
        )

    if is_db_path_in_project_dir():
        return (
            "⚠️ База лежит в папке проекта. При обновлении с Git или редеплое "
            "эта папка может пересоздаваться, и орешки будут выглядеть как сброшенные."
        )

    return "✅ База настроена вне папки проекта."


def migrate_existing_db_to_persistent_path():
    old_db_path = os.path.join(BASE_DIR, "kolybelka.db")

    if os.path.abspath(DB_PATH) == os.path.abspath(old_db_path):
        return

    if not os.path.exists(old_db_path) or os.path.exists(DB_PATH):
        return

    try:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        shutil.copy2(old_db_path, DB_PATH)
        print(f"SQLite база перенесена в постоянное хранилище: {DB_PATH}")
    except OSError as error:
        print("Не удалось перенести SQLite базу в постоянное хранилище:", error)


def read_legacy_db_rows(db_path, table_name):
    if not os.path.exists(db_path):
        return []

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(f"SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,))

        if not cur.fetchone():
            return []

        return [dict(row) for row in cur.execute(f"SELECT * FROM {table_name}").fetchall()]
    except sqlite3.Error as error:
        print(f"Не удалось прочитать старую SQLite таблицу {table_name}:", error)
        return []
    finally:
        try:
            conn.close()
        except UnboundLocalError:
            pass


def merge_legacy_db_into_current_db():
    old_db_path = os.path.join(BASE_DIR, "kolybelka.db")

    if os.path.abspath(DB_PATH) == os.path.abspath(old_db_path):
        return

    if not os.path.exists(old_db_path):
        return

    legacy_users = read_legacy_db_rows(old_db_path, "users")
    legacy_payments = read_legacy_db_rows(old_db_path, "payments")

    if not legacy_users and not legacy_payments:
        return

    imported_users = 0
    imported_payments = 0

    with db_connection() as conn:
        for user in legacy_users:
            user_id = user.get("user_id")

            if user_id is None:
                continue

            cur = conn.execute("""
                INSERT OR IGNORE INTO users (
                    user_id, username, nuts, lullabies, last_seen_at,
                    last_lullaby_at, reminders_enabled, last_reminder_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                user.get("username"),
                user.get("nuts", 0) or 0,
                user.get("lullabies", 0) or 0,
                user.get("last_seen_at"),
                user.get("last_lullaby_at"),
                user.get("reminders_enabled", 1),
                user.get("last_reminder_at"),
            ))

            if cur.rowcount:
                imported_users += 1
            else:
                conn.execute("""
                    UPDATE users
                    SET username = COALESCE(username, ?),
                        nuts = MAX(nuts, ?),
                        lullabies = MAX(lullabies, ?),
                        last_seen_at = COALESCE(last_seen_at, ?),
                        last_lullaby_at = COALESCE(last_lullaby_at, ?),
                        reminders_enabled = COALESCE(reminders_enabled, ?),
                        last_reminder_at = COALESCE(last_reminder_at, ?)
                    WHERE user_id = ?
                """, (
                    user.get("username"),
                    user.get("nuts", 0) or 0,
                    user.get("lullabies", 0) or 0,
                    user.get("last_seen_at"),
                    user.get("last_lullaby_at"),
                    user.get("reminders_enabled", 1),
                    user.get("last_reminder_at"),
                    user_id,
                ))

        for payment in legacy_payments:
            local_payment_id = payment.get("local_payment_id")

            if not local_payment_id or payment.get("user_id") is None:
                continue

            cur = conn.execute("""
                INSERT OR IGNORE INTO payments (
                    local_payment_id, yookassa_payment_id, user_id, package_key,
                    package_title, nuts, lullabies, amount_value, currency, status,
                    confirmation_url, customer_email, credited, created_at, paid_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                local_payment_id,
                payment.get("yookassa_payment_id"),
                payment.get("user_id"),
                payment.get("package_key", ""),
                payment.get("package_title", ""),
                payment.get("nuts", 0) or 0,
                payment.get("lullabies", 0) or 0,
                payment.get("amount_value", "0.00"),
                payment.get("currency", "RUB"),
                payment.get("status", "created"),
                payment.get("confirmation_url"),
                payment.get("customer_email"),
                payment.get("credited", 0) or 0,
                payment.get("created_at"),
                payment.get("paid_at"),
            ))

            if cur.rowcount:
                imported_payments += 1

    if imported_users or imported_payments:
        print(
            "SQLite база объединена со старой базой: "
            f"пользователей {imported_users}, платежей {imported_payments}"
        )


def ensure_persistence_dir():
    os.makedirs(os.path.dirname(os.path.abspath(PERSISTENCE_PATH)), exist_ok=True)

MAX_EDITS = 5
NUTS_PER_GENERATION = 1

NUT_PACKAGES = {
    "🌰 Купить 1 орешек": {
        "nuts": 1,
        "price": "349.00",
        "title": "1 орешек",
        "receipt_title": "1 персональная музыкальная колыбельная",
    },
    "🌰 Купить 2 орешка": {
        "nuts": 2,
        "price": "499.00",
        "title": "2 орешка",
        "receipt_title": "2 персональные музыкальные колыбельные",
    },
    "🌰 Купить 3 орешка": {
        "nuts": 3,
        "price": "599.00",
        "title": "3 орешка",
        "receipt_title": "3 персональные музыкальные колыбельные",
    },
}

(
    START,
    NAME_INPUT,
    NAME_CONFIRM,
    GENDER_INPUT,
    AGE_INPUT,
    AGE_CONFIRM,
    CHAR_INPUT,
    CHAR_CONFIRM,
    VOICE,
    MOOD,
    THEME,
    THEME_CUSTOM,
    FINAL_CONFIRM,
    LULLABY_REVIEW,
    EDIT_REQUEST,
    GENERATE_MUSIC,
    PAYMENT_EMAIL_INPUT,
    PROFILE_VIEW,
) = range(18)


BAD_WORDS = [
    "дурак", "дура", "идиот", "идиотка", "тупой", "тупая",
    "блять", "блядь", "сука", "хуй", "пизда", "ебать",
    "fuck", "shit", "bitch"
]

UNSAFE_CHILD_TOPICS = [
    "алкоголь", "водка", "вино", "пиво", "бар", "тусовка",
    "наркотик", "наркотики", "кокаин", "героин", "меф",
    "стрельба", "стрелять", "оружие", "пистолет", "автомат", "нож",
    "война", "бомба", "взрыв", "убийство", "убить", "кровь",
    "смерть", "ужас", "страх", "демон", "ад",
    "секс", "эротика", "порно",
    "казино", "ставки", "азарт",
]

UNSAFE_CHILD_PHRASES = [
    "ночной клуб",
    "алкогольный бар",
    "взрослая тусовка",
]

RUSSIAN_VOWELS_UPPER = "АЕЁИОУЫЭЮЯ"
STRESS_MARK = "\u0301"


# =========================
# КНОПКИ
# =========================

def keyboard(buttons, with_nav=True):
    final_buttons = list(buttons)

    if with_nav:
        final_buttons.append(["⬅️ Назад", "🔄 Начать заново"])
    else:
        final_buttons.append(["🔄 Начать заново"])

    return ReplyKeyboardMarkup(
        final_buttons,
        resize_keyboard=True,
        one_time_keyboard=False
    )


def main_menu_keyboard():
    return keyboard([
        ["🌙 Создать новую колыбельную"],
        ["👤 Личный кабинет"],
    ], with_nav=False)


def profile_keyboard():
    return keyboard([
        ["🌰 Купить орешки"],
        ["🌙 Создать новую колыбельную"],
        ["🏠 Главное меню"],
    ], with_nav=False)


def flow_profile_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🌰 Купить орешки"],
            ["⬅️ Вернуться назад"],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def buy_keyboard():
    return keyboard([
        ["🌰 Купить 1 орешек"],
        ["🌰 Купить 2 орешка"],
        ["🌰 Купить 3 орешка"],
        ["🏠 Главное меню"],
    ], with_nav=False)


def flow_buy_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🌰 Купить 1 орешек"],
            ["🌰 Купить 2 орешка"],
            ["🌰 Купить 3 орешка"],
            ["⬅️ Вернуться назад"],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def yes_change_keyboard():
    return keyboard([
        ["✅ Всё верно"],
        ["✏️ Изменить"],
    ])


def gender_keyboard():
    return keyboard([
        ["👧 Девочка"],
        ["👦 Мальчик"],
    ])


def generation_wait_keyboard():
    return keyboard([
        ["⏳ Жду результат"],
    ])


def text_review_keyboard():
    return keyboard([
        ["✅ Подтвердить"],
        ["✏️ Редактировать"],
    ])


def create_music_keyboard():
    return keyboard([
        ["🎵 Создать музыку"],
        ["🏠 Главное меню"],
    ])


# =========================
# БАЗА ДАННЫХ
# =========================

@contextmanager
def db_connection():
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db_connection() as conn:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                nuts INTEGER DEFAULT 0,
                lullabies INTEGER DEFAULT 0,
                last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_lullaby_at TEXT,
                reminders_enabled INTEGER NOT NULL DEFAULT 1,
                last_reminder_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                local_payment_id TEXT PRIMARY KEY,
                yookassa_payment_id TEXT,
                user_id INTEGER NOT NULL,
                package_key TEXT NOT NULL,
                package_title TEXT NOT NULL,
                nuts INTEGER NOT NULL,
                lullabies INTEGER DEFAULT 0,
                amount_value TEXT NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',
                status TEXT NOT NULL DEFAULT 'created',
                confirmation_url TEXT,
                customer_email TEXT,
                credited INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                paid_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS storage_probe (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                token TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                checked_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        ensure_column(cur, "users", "lullabies", "INTEGER DEFAULT 0")
        ensure_column(cur, "users", "last_seen_at", "TEXT")
        ensure_column(cur, "users", "last_lullaby_at", "TEXT")
        ensure_column(cur, "users", "reminders_enabled", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(cur, "users", "last_reminder_at", "TEXT")
        ensure_column(cur, "payments", "lullabies", "INTEGER DEFAULT 0")
        ensure_column(cur, "payments", "customer_email", "TEXT")

        cur.execute("""
            UPDATE users
            SET last_seen_at = CURRENT_TIMESTAMP
            WHERE last_seen_at IS NULL
        """)


def ensure_column(cur, table_name, column_name, column_definition):
    cur.execute(f"PRAGMA table_info({table_name})")
    existing_columns = {row[1] for row in cur.fetchall()}

    if column_name not in existing_columns:
        cur.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
        )


def create_user_if_not_exists(user):
    with db_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO users (user_id, username, nuts)
            VALUES (?, ?, 0)
        """, (user.id, user.username))
        conn.execute("""
            UPDATE users
            SET username = ?,
                last_seen_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (user.username, user.id))


def create_user_id_if_not_exists(user_id):
    with db_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO users (user_id, username, nuts)
            VALUES (?, NULL, 0)
        """, (user_id,))


def mark_lullaby_created(user_id):
    with db_connection() as conn:
        conn.execute("""
            UPDATE users
            SET last_lullaby_at = CURRENT_TIMESTAMP,
                last_seen_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (user_id,))


def set_reminders_enabled(user_id, enabled):
    create_user_id_if_not_exists(user_id)

    with db_connection() as conn:
        conn.execute("""
            UPDATE users
            SET reminders_enabled = ?
            WHERE user_id = ?
        """, (1 if enabled else 0, user_id))


def get_all_user_ids():
    with db_connection() as conn:
        rows = conn.execute("""
            SELECT user_id
            FROM users
            ORDER BY user_id
        """).fetchall()

    return [row[0] for row in rows]


def get_user_summaries(limit=20):
    with db_connection() as conn:
        rows = conn.execute("""
            SELECT user_id, username, nuts, lullabies, last_seen_at
            FROM users
            ORDER BY last_seen_at DESC, user_id
            LIMIT ?
        """, (limit,)).fetchall()

    return [
        {
            "user_id": row[0],
            "username": row[1],
            "nuts": row[2],
            "lullabies": row[3],
            "last_seen_at": row[4],
        }
        for row in rows
    ]


def get_database_stats():
    with db_connection() as conn:
        users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        payments_count = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        total_nuts = conn.execute("SELECT COALESCE(SUM(nuts), 0) FROM users").fetchone()[0]
        db_user_version = conn.execute("PRAGMA user_version").fetchone()[0]

    db_exists = os.path.exists(DB_PATH)
    db_size = os.path.getsize(DB_PATH) if db_exists else 0

    return {
        "db_path": DB_PATH,
        "db_exists": db_exists,
        "db_size": db_size,
        "users_count": users_count,
        "payments_count": payments_count,
        "total_nuts": total_nuts,
        "db_user_version": db_user_version,
        "base_dir": BASE_DIR,
        "shared_dir": os.getenv("SHARED_DIR", ""),
        "persistence_path": PERSISTENCE_PATH,
        "db_path_configured": is_db_path_configured_explicitly(),
        "db_path_in_project_dir": is_db_path_in_project_dir(),
        "db_path_in_shared_dir": is_db_path_in_shared_dir(),
        "db_persistence_warning": get_db_persistence_warning(),
    }


def get_or_create_storage_probe():
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    probe_file_path = os.path.join(db_dir, "kolybelka_storage_probe.txt")

    with db_connection() as conn:
        row = conn.execute("""
            SELECT token, created_at
            FROM storage_probe
            WHERE id = 1
        """).fetchone()

        if row:
            token, created_at = row
            conn.execute("""
                UPDATE storage_probe
                SET checked_at = CURRENT_TIMESTAMP
                WHERE id = 1
            """)
        else:
            token = uuid.uuid4().hex
            conn.execute("""
                INSERT INTO storage_probe (id, token)
                VALUES (1, ?)
            """, (token,))
            created_at = conn.execute("""
                SELECT created_at
                FROM storage_probe
                WHERE id = 1
            """).fetchone()[0]

    file_token = None
    file_error = None

    try:
        if os.path.exists(probe_file_path):
            with open(probe_file_path, "r", encoding="utf-8") as file:
                file_token = file.read().strip()
        else:
            with open(probe_file_path, "w", encoding="utf-8") as file:
                file.write(token)
            file_token = token
    except OSError as error:
        file_error = str(error)

    return {
        "db_token": token,
        "db_created_at": created_at,
        "probe_file_path": probe_file_path,
        "file_token": file_token,
        "file_error": file_error,
        "tokens_match": file_token == token,
    }


def get_users_for_reminder():
    with db_connection() as conn:
        rows = conn.execute("""
            SELECT user_id
            FROM users
            WHERE reminders_enabled = 1
              AND COALESCE(last_lullaby_at, last_seen_at, '1970-01-01 00:00:00') <= datetime('now', ?)
              AND (
                    last_reminder_at IS NULL
                    OR last_reminder_at <= datetime('now', ?)
                  )
            ORDER BY last_reminder_at IS NOT NULL, last_lullaby_at IS NOT NULL, user_id
        """, (
            f"-{REMINDER_AFTER_DAYS} days",
            f"-{REMINDER_INTERVAL_HOURS} hours",
        )).fetchall()

    return [row[0] for row in rows]


def mark_reminder_sent(user_id):
    with db_connection() as conn:
        conn.execute("""
            UPDATE users
            SET last_reminder_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (user_id,))


def get_nuts(user_id):
    with db_connection() as conn:
        row = conn.execute(
            "SELECT nuts FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

    return row[0] if row else 0


def get_lullabies(user_id):
    with db_connection() as conn:
        row = conn.execute(
            "SELECT lullabies FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

    return row[0] if row else 0


def add_nuts(user_id, amount):
    with db_connection() as conn:
        conn.execute("""
            UPDATE users
            SET nuts = nuts + ?
            WHERE user_id = ?
        """, (amount, user_id))


def add_lullabies(user_id, amount):
    with db_connection() as conn:
        conn.execute("""
            UPDATE users
            SET lullabies = lullabies + ?
            WHERE user_id = ?
        """, (amount, user_id))


def remove_nuts(user_id, amount):
    with db_connection() as conn:
        cur = conn.execute("""
            UPDATE users
            SET nuts = nuts - ?
            WHERE user_id = ? AND nuts >= ?
        """, (amount, user_id, amount))
        changed = cur.rowcount

    return changed > 0


def remove_lullabies(user_id, amount):
    with db_connection() as conn:
        cur = conn.execute("""
            UPDATE users
            SET lullabies = lullabies - ?
            WHERE user_id = ? AND lullabies >= ?
        """, (amount, user_id, amount))
        changed = cur.rowcount

    return changed > 0


def create_local_payment_order(user_id, package_key, customer_email):
    package = NUT_PACKAGES[package_key]
    local_payment_id = f"nuts_{uuid.uuid4().hex}"

    with db_connection() as conn:
        conn.execute("""
            INSERT INTO payments (
                local_payment_id, user_id, package_key, package_title,
                nuts, lullabies, amount_value, currency, status, customer_email
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'RUB', 'created', ?)
        """, (
            local_payment_id,
            user_id,
            package_key,
            package["title"],
            package["nuts"],
            0,
            package["price"],
            customer_email,
        ))

    return local_payment_id


def update_payment_order(local_payment_id, yookassa_payment_id, status, confirmation_url):
    with db_connection() as conn:
        conn.execute("""
            UPDATE payments
            SET yookassa_payment_id = ?,
                status = ?,
                confirmation_url = ?
            WHERE local_payment_id = ?
        """, (yookassa_payment_id, status, confirmation_url, local_payment_id))


def mark_payment_status(local_payment_id, status):
    with db_connection() as conn:
        conn.execute("""
            UPDATE payments
            SET status = ?
            WHERE local_payment_id = ?
        """, (status, local_payment_id))


def mark_payment_status_by_yookassa_id(yookassa_payment_id, status):
    with db_connection() as conn:
        conn.execute("""
            UPDATE payments
            SET status = ?
            WHERE yookassa_payment_id = ?
        """, (status, yookassa_payment_id))


def make_nuts_title(nuts):
    if 10 <= nuts % 100 <= 20:
        suffix = "орешков"
    elif nuts % 10 == 1:
        suffix = "орешек"
    elif 2 <= nuts % 10 <= 4:
        suffix = "орешка"
    else:
        suffix = "орешков"

    return f"{nuts} {suffix}"


def get_package_info_by_nuts(nuts):
    for package_key, package in NUT_PACKAGES.items():
        if package["nuts"] == nuts:
            return package_key, package["title"]

    title = make_nuts_title(nuts)
    return title, title


def recover_payment_order_from_yookassa(confirmed_payment, fallback_local_payment_id=None):
    yookassa_payment_id = confirmed_payment.get("id")
    metadata = confirmed_payment.get("metadata") or {}
    local_payment_id = metadata.get("local_payment_id") or fallback_local_payment_id

    if not yookassa_payment_id or not local_payment_id:
        return None

    try:
        user_id = int(metadata.get("user_id"))
        nuts = int(metadata.get("nuts"))
    except (TypeError, ValueError):
        return None

    if nuts <= 0:
        return None

    amount = confirmed_payment.get("amount") or {}
    amount_value = amount.get("value") or "0.00"
    currency = amount.get("currency") or "RUB"
    package_key, package_title = get_package_info_by_nuts(nuts)

    create_user_id_if_not_exists(user_id)

    with db_connection() as conn:
        row = conn.execute("""
            SELECT local_payment_id, yookassa_payment_id
            FROM payments
            WHERE local_payment_id = ? OR yookassa_payment_id = ?
            LIMIT 1
        """, (local_payment_id, yookassa_payment_id)).fetchone()

        if row:
            existing_local_payment_id, existing_yookassa_payment_id = row

            if existing_yookassa_payment_id and existing_yookassa_payment_id != yookassa_payment_id:
                return None

            conn.execute("""
                UPDATE payments
                SET yookassa_payment_id = ?,
                    status = 'succeeded',
                    amount_value = ?,
                    currency = ?
                WHERE local_payment_id = ?
            """, (
                yookassa_payment_id,
                amount_value,
                currency,
                existing_local_payment_id,
            ))

            return existing_local_payment_id

        conn.execute("""
            INSERT INTO payments (
                local_payment_id, yookassa_payment_id, user_id, package_key,
                package_title, nuts, lullabies, amount_value, currency,
                status, credited, paid_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 'succeeded', 0, CURRENT_TIMESTAMP)
        """, (
            local_payment_id,
            yookassa_payment_id,
            user_id,
            package_key,
            package_title,
            nuts,
            amount_value,
            currency,
        ))

    return local_payment_id


def credit_payment_if_needed(local_payment_id, yookassa_payment_id):
    with db_connection() as conn:
        row = conn.execute("""
            SELECT user_id, nuts, credited
            FROM payments
            WHERE local_payment_id = ? AND yookassa_payment_id = ?
        """, (local_payment_id, yookassa_payment_id)).fetchone()

        if not row:
            return None, False

        user_id, nuts, credited = row

        if credited:
            return {"user_id": user_id, "nuts": nuts}, False

        conn.execute("""
            UPDATE users
            SET nuts = nuts + ?
            WHERE user_id = ?
        """, (nuts, user_id))

        conn.execute("""
            UPDATE payments
            SET status = 'succeeded',
                credited = 1,
                paid_at = CURRENT_TIMESTAMP
            WHERE local_payment_id = ?
        """, (local_payment_id,))

    return {"user_id": user_id, "nuts": nuts}, True


# =========================
# ВАЛИДАЦИЯ
# =========================

def is_restart(text):
    return text in [
        "🔄 Начать заново",
        "🔄 Начать сначала",
        "Начать заново",
        "Начать сначала",
    ]


def is_back(text):
    return text in [
        "⬅️ Назад",
        "⬅️ Вернуться назад",
        "Назад",
        "Вернуться назад",
    ]


def is_flow_return(text):
    return text in [
        "⬅️ Вернуться назад",
        "Вернуться назад",
    ]


def is_yes(text):
    return text in ["✅ Всё верно", "Да"]


def is_change(text):
    return text in ["✏️ Изменить", "Изменить"]


def is_home(text):
    return text in ["🏠 Главное меню", "Главное меню"]


def is_create_lullaby(text):
    return text in [
        "🌙 Создать колыбельную",
        "Создать колыбельную",
        "🌙 Создать новую колыбельную",
        "Создать новую колыбельную",
    ]


def has_bad_words(text):
    low = text.lower()
    return any(word in low for word in BAD_WORDS)


def find_unsafe_topics(text):
    low = text.lower()
    words = set(re.findall(r"[а-яёa-z]+", low))
    found = [word for word in UNSAFE_CHILD_TOPICS if word in words]
    found.extend(phrase for phrase in UNSAFE_CHILD_PHRASES if phrase in low)
    return found


def validate_child_safe_text(text):
    unsafe = find_unsafe_topics(text)

    if unsafe:
        return False, (
            "🌙 Эта тема не подходит для детской колыбельной.\n\n"
            "Колыбелка бережёт сон малыша, поэтому я не могу использовать темы про "
            "алкоголь, войну, оружие, страх, насилие и другие взрослые сюжеты.\n\n"
            "✨ Напиши, пожалуйста, мягкую детскую тему.\n\n"
            "Например:\n"
            "про звёзды и луну\n"
            "про мишку и облака\n"
            "про домик у озера"
        )

    return True, ""


def validate_common(text):
    if not text or not text.strip():
        return False, "🌙 Поле не может быть пустым. Напиши, пожалуйста, пару слов."

    if len(text) > 300:
        return False, "✨ Текст получился слишком длинным. Напиши чуть короче, чтобы песня звучала легко."

    if has_bad_words(text):
        return False, "🌙 Давай без грубых слов — мы создаём нежную детскую колыбельную."

    return True, ""


def validate_email(email):
    email = email.strip()

    if len(email) > 120:
        return False

    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email))


def make_stressed_name(name_text):
    text = name_text.strip()

    uppercase_vowels = [
        i for i, char in enumerate(text)
        if char in RUSSIAN_VOWELS_UPPER
    ]

    if not uppercase_vowels:
        return None, None, (
            "🌙 Чтобы колыбельная звучала красиво, нужно отметить ударение в имени.\n\n"
            "Напиши имя так, чтобы ударная гласная была БОЛЬШОЙ буквой.\n\n"
            "✨ Примеры:\n"
            "МилАна\n"
            "АртЁм\n"
            "МарсЭль\n"
            "СофИя\n"
            "МирОн\n"
            "Ева\n"
            "АлИсА"
        )

    if 0 in uppercase_vowels and text[0] in RUSSIAN_VOWELS_UPPER:
        if len(uppercase_vowels) == 1:
            index = uppercase_vowels[0]
        else:
            index = uppercase_vowels[1]
    else:
        if len(uppercase_vowels) != 1:
            return None, None, (
                "🌙 Чтобы колыбельная звучала красиво, нужно отметить только одну ударную гласную.\n\n"
                "Ударную гласную сделай БОЛЬШОЙ.\n\n"
                "✨ Примеры:\n"
                "МилАна\n"
                "АртЁм\n"
                "МарсЭль\n"
                "СофИя\n"
                "МирОн\n"
                "Ева\n"
                "АлИсА"
            )

        index = uppercase_vowels[0]

    plain_chars = []
    stressed_chars = []

    for i, char in enumerate(text):
        lower_char = char.lower()
        plain_char = lower_char

        if i != 0 and lower_char == "э":
            plain_char = "е"

        if i == index:
            plain_chars.append(plain_char)
            stressed_chars.append(lower_char + STRESS_MARK)
        else:
            plain_chars.append(plain_char)
            stressed_chars.append(lower_char)

    plain_name = "".join(plain_chars).capitalize()
    stressed_name = "".join(stressed_chars).capitalize()

    return plain_name, stressed_name, ""


def make_genitive_name(name):
    name = name.strip()

    if not name:
        return name

    lower = name.lower()

    special_names = {
        "ева": "Евы",
        "саша": "Саши",
        "женя": "Жени",
        "кира": "Киры",
        "милана": "Миланы",
        "софия": "Софии",
        "артём": "Артёма",
        "артем": "Артема",
        "мирон": "Мирона",
        "марк": "Марка",
        "лев": "Льва",
        "павел": "Павла",
        "любовь": "Любови",
        "алиса": "Алисы",
        "анна": "Анны",
        "мария": "Марии",
        "дарья": "Дарьи",
        "ксения": "Ксении",
        "удмуртия": "Удмуртии",
    }

    if lower in special_names:
        return special_names[lower]

    if lower.endswith("ия"):
        return name[:-1] + "и"

    if lower.endswith("ья"):
        return name[:-1] + "и"

    if lower.endswith(("а", "я")):
        if lower.endswith(("га", "ка", "ха", "жа", "ча", "ша", "ща")):
            return name[:-1] + "и"
        return name[:-1] + "ы"

    if lower.endswith("ь"):
        return name[:-1] + "и"

    if lower.endswith("й"):
        return name[:-1] + "я"

    if lower.endswith(("н", "м", "р", "т", "в", "с", "л", "д", "п", "б", "г", "к", "з")):
        return name + "а"

    return name


def validate_name(name):
    ok, message = validate_common(name)
    if not ok:
        return False, message

    if len(name.strip()) < 2:
        return False, "🌙 Имя слишком короткое. Напиши имя ребёнка полностью."

    if len(name.strip()) > 30:
        return False, "✨ Имя получилось слишком длинным. Напиши только имя ребёнка."

    if not re.fullmatch(r"[А-Яа-яЁёA-Za-z\- ]+", name.strip()):
        return False, "🌙 В имени можно использовать только буквы."

    plain_name, stressed_name, stress_error = make_stressed_name(name)

    if stress_error:
        return False, stress_error

    return True, ""


def parse_age(age_text):
    age_text = age_text.strip().replace(".", ",")

    if not re.fullmatch(r"\d{1,2}(,\d{1,2})?", age_text):
        return None

    parts = age_text.split(",")
    years = int(parts[0])
    months = int(parts[1]) if len(parts) == 2 else 0

    if years < 0 or years > 12:
        return None

    if months < 0 or months > 11:
        return None

    if years == 12 and months > 0:
        return None

    if months == 0:
        display = f"{years} лет"
    else:
        display = f"{years} лет {months} месяцев"

    return {
        "raw": age_text,
        "years": years,
        "months": months,
        "display": display,
    }


def validate_age(age):
    ok, message = validate_common(age)
    if not ok:
        return False, message

    parsed = parse_age(age)

    if not parsed:
        return False, (
            "🌙 Возраст нужно написать числом.\n\n"
            "✨ Примеры:\n"
            "2 — если ребёнку 2 года\n"
            "3,5 — если 3 года 5 месяцев\n"
            "3,10 — если 3 года 10 месяцев\n\n"
            "После запятой указываются месяцы от 0 до 11."
        )

    return True, ""


def clean_filename(text):
    text = re.sub(r'[\\/*?:"<>|]', "", text)
    return text.strip()[:80]


def format_price(price):
    return price[:-3] if price.endswith(".00") else price


def seconds_left(started_at, max_seconds):
    return max_seconds - (time.monotonic() - started_at)


async def send_long_text(update: Update, text: str):
    max_length = 3500
    for i in range(0, len(text), max_length):
        await update.message.reply_text(text[i:i + max_length])


CREATE_TEXT_WAIT_MESSAGES = [
    (20, "🐿️ Колыбелка подбирает самые нежные слова для текста 🌙"),
    (50, "✨ Текст складывается в мягкий ритм"),
    (90, "🌙 Почти готово... проверяю строки и ударения"),
]

EDIT_TEXT_WAIT_MESSAGES = [
    (20, "✏️ Колыбелка уже вносит правки в текст 🌙"),
    (50, "✨ Ещё немного... подправляю строки, чтобы текст звучал мягче"),
    (90, "🌙 Почти готово... проверяю новые слова, окончания и ударения"),
]


async def run_with_wait_messages(update: Update, func, *args, wait_messages=None, max_seconds=None):
    task = asyncio.create_task(asyncio.to_thread(func, *args))

    if wait_messages is None:
        wait_messages = CREATE_TEXT_WAIT_MESSAGES

    elapsed = 0

    for seconds, message in wait_messages:
        if max_seconds is not None and seconds > max_seconds:
            break

        delay = seconds - elapsed

        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=delay)
        except asyncio.TimeoutError:
            elapsed = seconds
            await update.message.reply_text(
                message,
                reply_markup=generation_wait_keyboard()
            )

    if max_seconds is None:
        return await task

    remaining = max_seconds - elapsed

    if remaining <= 0:
        task.cancel()
        raise asyncio.TimeoutError()

    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except asyncio.TimeoutError:
        task.cancel()
        raise


# =========================
# OPENAI
# =========================

def get_openai_client():
    global OPENAI_CLIENT

    if OPENAI_CLIENT is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY не найден")

        OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY)

    return OPENAI_CLIENT


def get_age_context(data):
    years = data.get("age_years", 0)

    if years <= 2:
        return "ребёнок совсем маленький, нужны очень простые слова, мягкие повторы, короткие фразы"
    if years <= 5:
        return "ребёнок дошкольного возраста, можно использовать простые сказочные образы"
    if years <= 8:
        return "ребёнок уже понимает лёгкий сюжет, можно добавить небольшую историю"

    return "ребёнок постарше, можно сделать текст чуть более образным, но всё равно спокойным"


def get_gender_context(data):
    gender = data.get("gender", "")

    if gender == "👧 Девочка":
        return (
            "ребёнок — девочка. В тексте используй женский род: "
            "уснула, маленькая, добрая, любимая, смотрела, играла, заснула"
        )

    if gender == "👦 Мальчик":
        return (
            "ребёнок — мальчик. В тексте используй мужской род: "
            "уснул, маленький, добрый, любимый, смотрел, играл, заснул"
        )

    return "пол ребёнка не указан, избегай форм, где нужно выбирать мужской или женский род"


def get_name_pronunciation_context(data):
    name = data.get("name", "")
    name_stressed = data.get("name_stressed", name)

    return (
        f"Обычное написание имени: {name}. "
        f"Для песни используй вокальное написание с ударением: {name_stressed}. "
        "Если в вокальном написании есть буква «э», это фонетическая подсказка "
        "для правильного пения имени, например Марсэ́ль."
    )


def get_character_context(data):
    characters = data.get("characters", "")

    if characters == "без персонажей":
        return "Пользователь попросил песню без персонажей: не добавляй персонажей."

    return (
        "Если пользователь указал известных персонажей, героев мультфильмов, "
        "спортсменов или других публичных людей, можно упомянуть их в мягком "
        "детском контексте как образ мечты, игры или вдохновения. "
        "Обязательно используй указанных персонажей и имена, если они безопасны "
        "для детской колыбельной. Не утверждай, что реальная публичная личность "
        "лично участвует в песне, не говори от её лица и не имитируй её голос."
    )


def generate_lullaby_text(data):
    age_context = get_age_context(data)
    gender_context = get_gender_context(data)
    name_context = get_name_pronunciation_context(data)
    character_context = get_character_context(data)

    prompt = f"""
Ты профессиональный автор детских колыбельных.

Создай детскую колыбельную на русском языке.

Данные:
Имя ребёнка: {data["name_stressed"]}
Фонетика имени: {name_context}
Пол ребёнка: {data["gender"]}
Возраст ребёнка: {data["age"]}
Возрастной контекст: {age_context}
Грамматический контекст пола: {gender_context}
Персонажи и образы: {data["characters"]}
Контекст персонажей: {character_context}
Голос для музыкальной версии: {data["voice"]}
Настроение: {data["mood"]}
Тема: {data["theme"]}

Структура:
- Куплет: 4 строки
- Припев: 8 строк, то есть 2 четверостишия
- Куплет: 4 строки
- Припев: повтори те же самые 8 строк без изменений

Правила:
- общая длина: 700–1200 символов
- припев должен состоять ровно из двух четверостиший
- оба повторения припева должны быть одинаковым текстом
- припев должен легко запоминаться и мягко петься
- НЕ пиши слова "Куплет" или "Припев"
- текст должен идти как обычная песня
- используй только имя ребёнка: {data["name_stressed"]}
- ударение в имени уже проставлено, сохраняй его во всех повторениях имени
- учитывай фонетику имени и не меняй вокальное написание имени
- обязательно учитывай пол ребёнка и ставь правильные окончания
- если ребёнок девочка, не используй мужские формы: уснул, смотрел, играл, маленький, добрый
- если ребёнок мальчик, не используй женские формы: уснула, смотрела, играла, маленькая, добрая
- ритм должен быть плавный и легко поющийся
- избегай длинных сложных слов
- мама, папа, бабушка, дедушка, герои мультфильмов и сказочные образы могут быть персонажами песни
- если указано "без персонажей", не добавляй персонажей
- персонажи должны быть добрыми и спокойными
- без плохих слов
- без страшных образов
- без тревожных слов
- без алкоголя, оружия, войны, насилия, смерти, наркотиков, взрослого контента
- текст должен быть мягкий, тёплый, безопасный
- не пиши пояснения
- верни только текст песни
"""

    response = get_openai_client().responses.create(
        model="gpt-5.2",
        input=prompt
    )

    return response.output_text


def edit_lullaby_text(data, old_text, edit_request):
    age_context = get_age_context(data)
    gender_context = get_gender_context(data)
    name_context = get_name_pronunciation_context(data)
    character_context = get_character_context(data)

    prompt = f"""
Перепиши детскую колыбельную с учётом правки пользователя.

Данные:
Имя ребёнка: {data["name_stressed"]}
Фонетика имени: {name_context}
Пол ребёнка: {data["gender"]}
Возраст ребёнка: {data["age"]}
Возрастной контекст: {age_context}
Грамматический контекст пола: {gender_context}
Персонажи: {data["characters"]}
Контекст персонажей: {character_context}
Голос для музыкальной версии: {data["voice"]}
Настроение: {data["mood"]}
Тема: {data["theme"]}

Текущий текст:
{old_text}

Правка пользователя:
{edit_request}

Правила:
- структура: Куплет 4 строки → Припев 8 строк → Куплет 4 строки → тот же Припев 8 строк
- припев должен состоять из 2 четверостиший
- оба повторения припева должны быть одинаковым текстом
- длина: 700–1200 символов
- НЕ пиши слова "Куплет" или "Припев"
- используй только имя ребёнка: {data["name_stressed"]}
- сохраняй ударение в имени
- сохраняй фонетическое написание имени
- обязательно учитывай пол ребёнка и ставь правильные окончания
- если ребёнок девочка, не используй мужские формы: уснул, смотрел, играл, маленький, добрый
- если ребёнок мальчик, не используй женские формы: уснула, смотрела, играла, маленькая, добрая
- текст должен легко петься
- избегай сложных слов
- сохрани мягкий формат колыбельной
- без страшных образов
- без плохих слов
- без алкоголя, оружия, войны, насилия, смерти, наркотиков, взрослого контента
- не пиши пояснения
- верни только новый текст песни
"""

    response = get_openai_client().responses.create(
        model="gpt-5.2",
        input=prompt
    )

    return response.output_text


def polish_lullaby_text(data, text):
    gender_context = get_gender_context(data)
    name_context = get_name_pronunciation_context(data)
    character_context = get_character_context(data)

    prompt = f"""
Проверь детскую колыбельную на русском языке.

Данные:
Имя ребёнка: {data["name_stressed"]}
Фонетика имени: {name_context}
Пол ребёнка: {data["gender"]}
Грамматический контекст пола: {gender_context}
Персонажи и образы: {data["characters"]}
Контекст персонажей: {character_context}

Задача:
- исправь грамматические ошибки
- исправь неестественные фразы
- улучши фонетику для пения
- упрости сложные слова
- структура должна быть: Куплет 4 строки → Припев 8 строк → Куплет 4 строки → тот же Припев 8 строк
- припев должен состоять из 2 четверостиший
- оба повторения припева должны быть одинаковым текстом
- сохрани мягкий ритм колыбельной
- сохрани имя ребёнка именно так: {data["name_stressed"]}
- сохрани фонетическое написание имени
- сохрани безопасных персонажей и образы, которые указал пользователь
- обязательно исправь окончания по полу ребёнка
- если ребёнок девочка, не используй мужские формы: уснул, смотрел, играл, маленький, добрый
- если ребёнок мальчик, не используй женские формы: уснула, смотрела, играла, маленькая, добрая
- полностью убери или замени любые недетские темы: алкоголь, бар, оружие, война, насилие, страх, смерть, наркотики, взрослый контент
- если встретились недетские темы, мягко замени их на безопасные детские образы: звёзды, луна, облака, сон, игрушки, мама, папа, зверята
- не меняй смысл песни сильнее необходимого
- не добавляй пояснения
- верни только текст песни

Текст:
{text}
"""

    response = get_openai_client().responses.create(
        model="gpt-5.2",
        input=prompt
    )

    return response.output_text


def add_stress_marks_to_song(data, text):
    gender_context = get_gender_context(data)
    name_context = get_name_pronunciation_context(data)

    prompt = f"""
Расставь ударения в тексте русской детской песни для музыкальной генерации Suno.

Данные:
Имя ребёнка: {data["name_stressed"]}
Фонетика имени: {name_context}
Пол ребёнка: {data["gender"]}
Грамматический контекст пола: {gender_context}

Важно:
- добавь знак ударения ́ после ударной гласной в каждом русском слове, где это возможно
- в каждом русском слове должно быть не больше одного знака ударения
- не ставь ударение в односложных служебных словах, если это звучит неестественно
- имя ребёнка всегда пиши именно так: {data["name_stressed"]}
- сохрани фонетическое написание имени
- не меняй слова
- не меняй строки
- не меняй смысл
- не меняй родовые окончания
- не добавляй пояснения
- не добавляй заголовки
- верни только текст песни с ударениями

Текст:
{text}
"""

    response = get_openai_client().responses.create(
        model="gpt-5.2",
        input=prompt
    )

    return response.output_text


def verify_stress_marks_for_song(data, text):
    gender_context = get_gender_context(data)
    name_context = get_name_pronunciation_context(data)

    prompt = f"""
Проведи финальную проверку ударений в русской детской песне для музыкальной генерации.

Данные:
Имя ребёнка: {data["name_stressed"]}
Фонетика имени: {name_context}
Пол ребёнка: {data["gender"]}
Грамматический контекст пола: {gender_context}

Задача:
- проверь каждое русское слово и исправь неверные ударения
- добавь пропущенные ударения в значимых русских словах
- в одном слове должен быть не больше одного знака ударения
- имя ребёнка всегда пиши именно так: {data["name_stressed"]}
- сохрани фонетическое написание имени
- не меняй слова, строки, смысл, порядок строк и родовые окончания
- не добавляй пояснения
- верни только исправленный текст песни с ударениями

Текст:
{text}
"""

    response = get_openai_client().responses.create(
        model="gpt-5.2",
        input=prompt
    )

    return response.output_text


def safety_repair_and_note(data, text):
    unsafe = find_unsafe_topics(text)

    if not unsafe:
        return text, ""

    repaired = polish_lullaby_text(data, text)
    return repaired, ""


def prepare_final_lyrics(data, raw_text):
    polished = polish_lullaby_text(data, raw_text)
    repaired, note = safety_repair_and_note(data, polished)
    stressed = add_stress_marks_to_song(data, repaired)
    verified = verify_stress_marks_for_song(data, stressed)

    return verified, note


def generate_and_prepare_lullaby(data):
    raw_text = generate_lullaby_text(data)
    return prepare_final_lyrics(data, raw_text)


def edit_and_prepare_lullaby(data, old_text, edit_request):
    raw_new_text = edit_lullaby_text(data, old_text, edit_request)
    return prepare_final_lyrics(data, raw_new_text)


# =========================
# SUNO
# =========================

def make_music_style(data):
    voice = data["voice"]

    if voice == "👩 Женский голос":
        voice_style = "soft female vocal, warm motherly voice, gentle woman singer"
    elif voice == "👨 Мужской голос":
        voice_style = (
            "male vocal only, soft male singer, warm fatherly voice, "
            "gentle low male voice, calm baritone lullaby, no female vocal, no woman voice"
        )
    else:
        voice_style = (
            "real child voice, very young kid singing, innocent child vocal, "
            "soft childlike pronunciation, not adult female voice, not woman voice"
        )

    return (
        f"Russian lullaby, {voice_style}, "
        f"slow calm tempo, gentle melody, soft piano, soft strings, "
        f"warm bedtime atmosphere, soothing, peaceful, tender"
    )


def create_music_task(lyrics, style, title):
    headers = {
        "Authorization": f"Bearer {SUNO_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "customMode": True,
        "instrumental": False,
        "model": "V5_5",
        "prompt": lyrics,
        "style": style,
        "title": title,
        "callBackUrl": "https://example.com/callback"
    }

    response = requests.post(
        f"{SUNO_BASE_URL}/api/v1/generate",
        headers=headers,
        json=data,
        timeout=60
    )

    response.raise_for_status()
    return response.json()["data"]["taskId"]


def get_music_audio_urls(task_id):
    headers = {
        "Authorization": f"Bearer {SUNO_API_KEY}"
    }

    response = requests.get(
        f"{SUNO_BASE_URL}/api/v1/generate/record-info",
        headers=headers,
        params={"taskId": task_id},
        timeout=60
    )

    response.raise_for_status()
    result = response.json()

    if result["data"]["status"] != "SUCCESS":
        return []

    songs = result["data"]["response"].get("sunoData", [])
    urls = []

    for song in songs:
        audio_url = song.get("audioUrl")
        stream_url = song.get("streamAudioUrl") or song.get("sourceStreamAudioUrl")

        if audio_url:
            urls.append(audio_url)
        elif stream_url:
            urls.append(stream_url)

    return urls


def download_audio(audio_url, filename):
    response = requests.get(audio_url, timeout=120)
    response.raise_for_status()

    path = os.path.join(tempfile.gettempdir(), filename)

    with open(path, "wb") as file:
        file.write(response.content)

    return path


# =========================
# ЮKASSA
# =========================

def yookassa_is_configured():
    return bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)


def yookassa_test_notice():
    if not YOOKASSA_TEST_MODE:
        return ""

    return "🧪 Тестовая оплата ЮKassa. Реальные деньги не списываются.\n\n"


def create_yookassa_payment(user_id, package_key, customer_email):
    package = NUT_PACKAGES[package_key]
    local_payment_id = create_local_payment_order(user_id, package_key, customer_email)

    payload = {
        "amount": {
            "value": package["price"],
            "currency": "RUB",
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": YOOKASSA_RETURN_URL,
        },
        "description": f"{BRAND_NAME}: {package['title']}",
        "metadata": {
            "local_payment_id": local_payment_id,
            "user_id": str(user_id),
            "nuts": str(package["nuts"]),
            "test_mode": "true" if YOOKASSA_TEST_MODE else "false",
        },
        "receipt": {
            "customer": {
                "email": customer_email,
            },
            "items": [
                {
                    "description": package["receipt_title"],
                    "quantity": "1.00",
                    "amount": {
                        "value": package["price"],
                        "currency": "RUB",
                    },
                    "vat_code": YOOKASSA_VAT_CODE,
                    "payment_subject": "service",
                    "payment_mode": "full_payment",
                }
            ],
        },
    }

    if YOOKASSA_TAX_SYSTEM_CODE:
        payload["receipt"]["tax_system_code"] = int(YOOKASSA_TAX_SYSTEM_CODE)

    response = requests.post(
        f"{YOOKASSA_BASE_URL}/payments",
        auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
        headers={
            "Content-Type": "application/json",
            "Idempotence-Key": local_payment_id,
        },
        json=payload,
        timeout=60,
    )

    try:
        response.raise_for_status()
    except requests.HTTPError:
        mark_payment_status(local_payment_id, "failed")
        raise

    result = response.json()
    confirmation_url = result.get("confirmation", {}).get("confirmation_url")

    update_payment_order(
        local_payment_id,
        result["id"],
        result["status"],
        confirmation_url,
    )

    return {
        "local_payment_id": local_payment_id,
        "yookassa_payment_id": result["id"],
        "status": result["status"],
        "confirmation_url": confirmation_url,
        "package": package,
    }


def get_yookassa_payment(yookassa_payment_id):
    response = requests.get(
        f"{YOOKASSA_BASE_URL}/payments/{yookassa_payment_id}",
        auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
        timeout=60,
    )

    response.raise_for_status()
    return response.json()


def process_yookassa_webhook(payload):
    event = payload.get("event")
    payment = payload.get("object") or {}
    yookassa_payment_id = payment.get("id")
    metadata = payment.get("metadata") or {}
    local_payment_id = metadata.get("local_payment_id")

    if event == "payment.succeeded":
        if not yookassa_payment_id:
            print("Webhook ЮKassa без id платежа:", payload)
            return {
                "action": "ignored",
                "event": event,
                "reason": "missing_payment_id",
            }

        confirmed_payment = get_yookassa_payment(yookassa_payment_id)
        confirmed_metadata = {
            **metadata,
            **(confirmed_payment.get("metadata") or {}),
        }
        confirmed_payment = {
            **confirmed_payment,
            "metadata": confirmed_metadata,
        }
        local_payment_id = (
            local_payment_id
            or confirmed_metadata.get("local_payment_id")
        )

        if confirmed_payment.get("status") != "succeeded" or not confirmed_payment.get("paid"):
            status = confirmed_payment.get("status") or "pending"

            if local_payment_id:
                mark_payment_status(local_payment_id, status)
            else:
                mark_payment_status_by_yookassa_id(yookassa_payment_id, status)

            return {
                "action": "not_paid",
                "yookassa_payment_id": yookassa_payment_id,
            }

        if not local_payment_id:
            local_payment_id = recover_payment_order_from_yookassa(confirmed_payment)

        if not local_payment_id:
            print(f"Webhook ЮKassa: не удалось восстановить заказ {yookassa_payment_id}")
            return {
                "action": "missing_order",
                "yookassa_payment_id": yookassa_payment_id,
            }

        order, credited_now = credit_payment_if_needed(
            local_payment_id,
            yookassa_payment_id,
        )

        if not order:
            local_payment_id = recover_payment_order_from_yookassa(
                confirmed_payment,
                local_payment_id,
            )
            order, credited_now = credit_payment_if_needed(
                local_payment_id,
                yookassa_payment_id,
            )

        if not order:
            print(f"Webhook ЮKassa: заказ для платежа {yookassa_payment_id} не найден")
            return {
                "action": "missing_order",
                "yookassa_payment_id": yookassa_payment_id,
            }

        return {
            "action": "credited" if credited_now else "already_credited",
            "order": order,
            "balance": get_nuts(order["user_id"]),
            "yookassa_payment_id": yookassa_payment_id,
        }

    if event == "payment.canceled":
        if local_payment_id:
            mark_payment_status(local_payment_id, "canceled")
        elif yookassa_payment_id:
            mark_payment_status_by_yookassa_id(yookassa_payment_id, "canceled")

        return {
            "action": "canceled",
            "yookassa_payment_id": yookassa_payment_id,
        }

    return {
        "action": "ignored",
        "event": event,
        "yookassa_payment_id": yookassa_payment_id,
    }


async def notify_payment_credited(app, result):
    if result.get("action") != "credited":
        return

    order = result["order"]
    await app.bot.send_message(
        chat_id=order["user_id"],
        text=(
            "✅ Оплата прошла успешно!\n\n"
            f"🌰 Начислено: {order['nuts']} орешков\n"
            f"Текущий баланс: {result['balance']} орешков\n\n"
            "Теперь можно создать персональную колыбельную 🌙🎵"
        ),
        reply_markup=profile_keyboard(),
    )


def schedule_payment_notification(app, result):
    if TELEGRAM_EVENT_LOOP and TELEGRAM_EVENT_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(
            notify_payment_credited(app, result),
            TELEGRAM_EVENT_LOOP,
        )
        return

    asyncio.run(notify_payment_credited(app, result))


def make_yookassa_webhook_handler(app):
    class YookassaWebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            if urlparse(self.path).path != YOOKASSA_WEBHOOK_PATH:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
                return

            if content_length <= 0 or content_length > 1024 * 1024:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid payload size")
                return

            try:
                body = self.rfile.read(content_length)
                payload = json.loads(body.decode("utf-8"))
                result = process_yookassa_webhook(payload)
            except json.JSONDecodeError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
                return
            except Exception as error:
                print("Ошибка webhook ЮKassa:", error)
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Webhook processing failed")
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))

            schedule_payment_notification(app, result)

        def log_message(self, format, *args):
            print("ЮKassa webhook:", format % args)

    return YookassaWebhookHandler


def start_yookassa_webhook_server(app):
    server = ThreadingHTTPServer(
        (YOOKASSA_WEBHOOK_HOST, YOOKASSA_WEBHOOK_PORT),
        make_yookassa_webhook_handler(app),
    )

    thread = threading.Thread(
        target=server.serve_forever,
        name="yookassa-webhook",
        daemon=True,
    )
    thread.start()

    print(
        "Webhook ЮKassa слушает "
        f"http://{YOOKASSA_WEBHOOK_HOST}:{YOOKASSA_WEBHOOK_PORT}{YOOKASSA_WEBHOOK_PATH}"
    )

    return server


async def send_payment_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    package_key,
    customer_email,
    reply_markup=None,
):
    if not yookassa_is_configured():
        await update.message.reply_text(
            "😔 Оплата пока не настроена. Попробуй позже.",
            reply_markup=profile_keyboard()
        )
        return

    try:
        payment = await asyncio.to_thread(
            create_yookassa_payment,
            update.effective_user.id,
            package_key,
            customer_email,
        )
    except Exception as error:
        print("Ошибка создания платежа ЮKassa:", error)
        await update.message.reply_text(
            "😔 Не получилось создать ссылку на оплату.\n\n"
            "Попробуй ещё раз чуть позже.",
            reply_markup=buy_keyboard()
        )
        return

    package = payment["package"]
    payment_url = payment["confirmation_url"]

    if not payment_url:
        await update.message.reply_text(
            "😔 ЮKassa не вернула ссылку на оплату.\n\n"
            "Попробуй выбрать количество орешков ещё раз.",
            reply_markup=buy_keyboard()
        )
        return

    await update.message.reply_text(
        yookassa_test_notice() +
        f"🌰 Орешки: {package['title']}\n"
        f"💳 Стоимость: 🔵 {format_price(package['price'])} ₽\n\n"
        f"Оплати по ссылке ЮKassa:\n{payment_url}\n\n"
        "После оплаты орешки начислятся автоматически, как только ЮKassa пришлёт подтверждение.",
        reply_markup=reply_markup or buy_keyboard()
    )


# =========================
# МЕНЮ
# =========================

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    create_user_if_not_exists(update.effective_user)

    await update.message.reply_text(
    f"🌙 Добро пожаловать в {BRAND_NAME}!\n\n"
    f"Я создаю персональные музыкальные колыбельные для детей 💫\n\n"
    f"🎵 В каждой песне:\n"
    f"— имя ребёнка\n"
    f"— любимые персонажи\n"
    f"— нежный голос и спокойная музыка\n\n"
    f"✨ Получается настоящая колыбельная, под которую ребёнок засыпает быстрее и спокойнее 🌙\n\n"
    f"🌰 1 орешек = 1 персональная музыкальная колыбельная\n\n"
    f"💛 Давай создадим первую прямо сейчас:",
    reply_markup=main_menu_keyboard()
    )

    return START


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    return await show_main_menu(update, context)


async def offer_buy_nuts(update: Update, user_id):
    nuts = get_nuts(user_id)

    await update.message.reply_text(
        f"🌙 На балансе пока нет орешков.\n\n"
        f"Сейчас доступно: {nuts}\n"
        f"Для создания колыбельной нужен 1 орешек.\n\n"
        f"Выбери количество орешков, и после оплаты можно будет сразу начать создание.",
        reply_markup=buy_keyboard()
    )


def save_profile_return_state(context: ContextTypes.DEFAULT_TYPE, source_state):
    if source_state is not None and source_state != START:
        context.user_data["_profile_return_state"] = source_state


async def show_profile(update: Update, user_id, contextual=False):
    nuts = get_nuts(user_id)

    await update.message.reply_text(
        f"👤 Личный кабинет\n\n"
        f"🌰 Твой баланс: {nuts} орешков\n\n"
        f"1 орешек = 1 персональная музыкальная колыбельная.\n\n"
        f"Здесь можно купить орешки"
        f"{' и вернуться к созданию колыбельной' if contextual else ' или сразу создать новую колыбельную 🌙'}",
        reply_markup=flow_profile_keyboard() if contextual else profile_keyboard()
    )

    return PROFILE_VIEW if contextual else START


async def show_buy_nuts_menu(update: Update, contextual=False):
    await update.message.reply_text(
        "🌰 Выбери количество орешков\n\n"
        "1 орешек — 🔵 349 ₽\n"
        "2 орешка — 🔵 499 ₽\n"
        "3 орешка — 🔵 599 ₽",
        reply_markup=flow_buy_keyboard() if contextual else buy_keyboard()
    )

    return PROFILE_VIEW if contextual else START


async def ask_payment_email(update: Update, context: ContextTypes.DEFAULT_TYPE, package_key):
    context.user_data["pending_payment_package_key"] = package_key

    await update.message.reply_text(
        "📧 Напиши электронную почту для отправки чека.",
        reply_markup=keyboard([])
    )

    return PAYMENT_EMAIL_INPUT


async def begin_lullaby_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    nuts = get_nuts(user_id)

    if nuts < NUTS_PER_GENERATION:
        await offer_buy_nuts(update, user_id)
        return START

    context.user_data.clear()

    await update.message.reply_text(
        "🌙 Давай создадим персональную колыбельную\n\n"
        "Сначала напиши имя ребёнка.\n\n"
        "✨ Важно: чтобы имя красиво звучало в песне, выдели ударную гласную БОЛЬШОЙ буквой.\n"
        "Если в имени буква «е» произносится как «э», напиши её как «Э».\n\n"
        "Примеры:\n"
        "МилАна\n"
        "КИра\n"
        "МарсЭль\n"
        "СофИя\n"
        "МирОн\n"
        "Ева\n"
        "АлИса",
        reply_markup=keyboard([])
    )

    return NAME_INPUT


async def show_state_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, state):
    if state == NAME_INPUT:
        await update.message.reply_text(
            "🌙 Вернёмся к имени ребёнка.\n\n"
            "Напиши имя и выдели ударную гласную БОЛЬШОЙ буквой.\n"
            "Например: МилАна или МарсЭль",
            reply_markup=keyboard([])
        )
        return NAME_INPUT

    if state == NAME_CONFIRM and context.user_data.get("pending_name"):
        await update.message.reply_text(
            f"👶 Имя ребёнка: {context.user_data['pending_name']}\n"
            f"🎵 Произношение для песни: {context.user_data.get('pending_name_stressed', context.user_data['pending_name'])}\n\n"
            "Так правильно?",
            reply_markup=yes_change_keyboard()
        )
        return NAME_CONFIRM

    if state == GENDER_INPUT:
        await update.message.reply_text(
            "👶 Вернёмся к выбору пола ребёнка.",
            reply_markup=gender_keyboard()
        )
        return GENDER_INPUT

    if state == AGE_INPUT:
        await update.message.reply_text(
            "🎂 Вернёмся к возрасту ребёнка.\n\n"
            "Например: 2 или 3,5",
            reply_markup=keyboard([])
        )
        return AGE_INPUT

    if state == AGE_CONFIRM and context.user_data.get("pending_age"):
        await update.message.reply_text(
            f"🎂 Возраст ребёнка: {context.user_data['pending_age']}\n\n"
            "Всё верно?",
            reply_markup=yes_change_keyboard()
        )
        return AGE_CONFIRM

    if state == CHAR_INPUT:
        await update.message.reply_text(
            "🧸 Вернёмся к персонажам.\n\n"
            "Напиши персонажей или нажми «🧸 Без персонажей».",
            reply_markup=keyboard([
                ["🧸 Без персонажей"]
            ])
        )
        return CHAR_INPUT

    if state == CHAR_CONFIRM and context.user_data.get("pending_characters"):
        await update.message.reply_text(
            f"🧸 Персонажи: {context.user_data['pending_characters']}\n\n"
            "Всё верно?",
            reply_markup=yes_change_keyboard()
        )
        return CHAR_CONFIRM

    if state == VOICE:
        await update.message.reply_text(
            "🎤 Вернёмся к выбору голоса:",
            reply_markup=keyboard([
                ["👩 Женский голос"],
                ["👨 Мужской голос"],
                ["🧒 Детский голос"]
            ])
        )
        return VOICE

    if state == MOOD:
        await update.message.reply_text(
            "✨ Вернёмся к настроению колыбельной:",
            reply_markup=keyboard([
                ["💗 Очень нежная"],
                ["🌟 Волшебная"],
                ["🌙 Спокойная"],
                ["🧚 Добрая сказочная"]
            ])
        )
        return MOOD

    if state == THEME:
        await update.message.reply_text(
            "🌌 Вернёмся к выбору темы:",
            reply_markup=keyboard([
                ["🌙 Звёзды и луна"],
                ["🌲 Лес и зверята"],
                ["☁️ Море и облака"],
                ["🧸 Игрушки засыпают"],
                ["🤍 Мама рядом"],
                ["💙 Папа рядом"],
                ["🌈 Свой вариант"]
            ])
        )
        return THEME

    if state == THEME_CUSTOM:
        await update.message.reply_text(
            "🌈 Вернёмся к своей теме колыбельной.\n\n"
            "Напиши мягкую детскую тему.",
            reply_markup=keyboard([])
        )
        return THEME_CUSTOM

    if state == FINAL_CONFIRM:
        return await show_final_summary(update, context)

    if state == LULLABY_REVIEW:
        await update.message.reply_text(
            "✨ Текст тебе нравится?",
            reply_markup=text_review_keyboard()
        )
        return LULLABY_REVIEW

    if state == EDIT_REQUEST:
        await update.message.reply_text(
            "✏️ Вернёмся к правке текста.\n\n"
            "Напиши, что изменить в колыбельной.",
            reply_markup=keyboard([])
        )
        return EDIT_REQUEST

    if state == GENERATE_MUSIC:
        await update.message.reply_text(
            "🎵 Вернёмся к созданию музыки.",
            reply_markup=create_music_keyboard()
        )
        return GENERATE_MUSIC

    if state == PAYMENT_EMAIL_INPUT:
        await update.message.reply_text(
            "📧 Вернёмся к оплате.\n\n"
            "Напиши электронную почту для отправки чека.",
            reply_markup=keyboard([])
        )
        return PAYMENT_EMAIL_INPUT

    return await show_main_menu(update, context)


async def return_to_saved_flow_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.pop("_profile_return_state", START)
    context.user_data.pop("_profile_subview", None)
    return await show_state_prompt(update, context, state)


async def global_button(update: Update, context: ContextTypes.DEFAULT_TYPE, source_state=None):
    text = update.message.text
    user_id = update.effective_user.id

    create_user_if_not_exists(update.effective_user)

    if is_restart(text):
        return await start(update, context)

    if is_home(text):
        return await show_main_menu(update, context)

    if text == "👤 Личный кабинет":
        save_profile_return_state(context, source_state)
        return await show_profile(update, user_id, contextual=source_state not in (None, START))

    if text == "🌰 Купить орешки":
        save_profile_return_state(context, source_state)
        return await show_buy_nuts_menu(update, contextual=source_state not in (None, START))

    if text in NUT_PACKAGES:
        return await ask_payment_email(update, context, text)

    if is_create_lullaby(text):
        return await begin_lullaby_creation(update, context)

    return START


# =========================
# СТАРТОВОЕ МЕНЮ / ЛИЧНЫЙ КАБИНЕТ
# =========================

async def start_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    create_user_if_not_exists(update.effective_user)

    if is_restart(text):
        return await start(update, context)

    if is_home(text) or is_back(text):
        return await show_main_menu(update, context)

    if text == "👤 Личный кабинет":
        return await show_profile(update, user_id)

    if text == "🌰 Купить орешки":
        return await show_buy_nuts_menu(update)

    if text in NUT_PACKAGES:
        return await ask_payment_email(update, context, text)

    if is_create_lullaby(text):
        return await begin_lullaby_creation(update, context)

    if text in [
        "✅ Подтвердить",
        "Подтвердить",
        "✏️ Редактировать",
        "Редактировать",
        "🎵 Создать музыку",
        "Создать музыку",
    ]:
        await update.message.reply_text(
            "🌙 Этот шаг уже устарел или бот был перезапущен.\n\n"
            "Начни создание заново, чтобы я не потеряла текст и правильно собрала музыку.",
            reply_markup=main_menu_keyboard()
        )
        return START

    await update.message.reply_text(
        "🌙 Пожалуйста, выбери действие кнопкой ниже.",
        reply_markup=main_menu_keyboard()
    )
    return START


async def profile_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_restart(text):
        context.user_data.pop("_profile_return_state", None)
        context.user_data.pop("_profile_subview", None)
        return await start(update, context)

    if is_flow_return(text) or is_back(text):
        return await return_to_saved_flow_step(update, context)

    if text == "🌰 Купить орешки":
        context.user_data["_profile_subview"] = "buy"
        return await show_buy_nuts_menu(update, contextual=True)

    if text in NUT_PACKAGES:
        context.user_data["pending_payment_from_profile"] = True
        return await ask_payment_email(update, context, text)

    if text == "👤 Личный кабинет":
        return await show_profile(update, update.effective_user.id, contextual=True)

    await update.message.reply_text(
        "🌙 Выбери действие кнопкой ниже.",
        reply_markup=flow_profile_keyboard()
    )
    return PROFILE_VIEW


async def payment_email_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    from_profile = context.user_data.get("pending_payment_from_profile")

    if is_restart(text):
        return await start(update, context)

    if (is_back(text) or is_home(text) or is_flow_return(text)) and from_profile:
        context.user_data.pop("pending_payment_package_key", None)
        context.user_data.pop("pending_payment_from_profile", None)
        return await show_profile(update, update.effective_user.id, contextual=True)

    if is_back(text) or is_home(text):
        context.user_data.pop("pending_payment_package_key", None)
        await update.message.reply_text(
            "🌰 Выбери количество орешков:",
            reply_markup=buy_keyboard()
        )
        return START

    if not validate_email(text):
        await update.message.reply_text(
            "🌙 Похоже, email написан с ошибкой.\n\n"
            "Напиши email ещё раз, например: name@mail.ru",
            reply_markup=keyboard([])
        )
        return PAYMENT_EMAIL_INPUT

    package_key = context.user_data.get("pending_payment_package_key")

    if package_key not in NUT_PACKAGES:
        await update.message.reply_text(
            "😔 Не нашла выбранное количество орешков.\n\n"
            "Выбери количество орешков ещё раз.",
            reply_markup=buy_keyboard()
        )
        return START

    context.user_data.pop("pending_payment_package_key", None)
    context.user_data.pop("pending_payment_from_profile", None)

    if from_profile:
        await send_payment_link(
            update,
            context,
            package_key,
            text,
            reply_markup=flow_profile_keyboard(),
        )
        return PROFILE_VIEW

    await send_payment_link(update, context, package_key, text)
    return START


# =========================
# ИМЯ
# =========================

async def name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_restart(text):
        return await start(update, context)

    if is_back(text) or is_home(text):
        return await show_main_menu(update, context)

    name = text.strip()
    ok, message = validate_name(name)

    if not ok:
        await update.message.reply_text(message, reply_markup=keyboard([]))
        return NAME_INPUT

    plain_name, stressed_name, stress_error = make_stressed_name(name)

    if stress_error:
        await update.message.reply_text(stress_error, reply_markup=keyboard([]))
        return NAME_INPUT

    context.user_data["pending_name"] = plain_name
    context.user_data["pending_name_stressed"] = stressed_name

    await update.message.reply_text(
        f"✨ Отлично, я запомнила имя!\n\n"
        f"👶 Имя ребёнка: {plain_name}\n"
        f"🎵 Произношение для песни: {stressed_name}\n\n"
        f"Так правильно?",
        reply_markup=yes_change_keyboard()
    )

    return NAME_CONFIRM


async def name_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_restart(text):
        return await start(update, context)

    if is_back(text) or is_change(text):
        await update.message.reply_text(
            "🌙 Напиши имя ребёнка заново.\n\n"
            "Ударную гласную выдели БОЛЬШОЙ буквой.\n"
            "Если «е» в имени звучит как «э», напиши эту букву как «Э».\n\n"
            "Примеры:\n"
            "МилАна\n"
            "АртЁм\n"
            "МарсЭль\n"
            "СофИя\n"
            "МирОн\n"
            "Ева\n"
            "АлИсА",
            reply_markup=keyboard([])
        )
        return NAME_INPUT

    if not is_yes(text):
        await update.message.reply_text(
            "🌙 Нажми «✅ Всё верно» или «✏️ Изменить».",
            reply_markup=yes_change_keyboard()
        )
        return NAME_CONFIRM

    context.user_data["name"] = context.user_data["pending_name"]
    context.user_data["name_stressed"] = context.user_data["pending_name_stressed"]

    await update.message.reply_text(
        "👶 Выбери пол ребёнка.\n\n"
        "Это нужно, чтобы в песне были правильные окончания: уснул или уснула, маленький или маленькая.",
        reply_markup=gender_keyboard()
    )

    return GENDER_INPUT


# =========================
# ПОЛ РЕБЁНКА
# =========================

async def gender_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_restart(text):
        return await start(update, context)

    if is_back(text):
        await update.message.reply_text(
            "🌙 Вернёмся к имени.\n\n"
            "Напиши имя ребёнка и выдели ударную гласную БОЛЬШОЙ буквой.\n"
            "Если «е» звучит как «э», напиши эту букву как «Э».\n\n"
            "Например: МилАна или МарсЭль",
            reply_markup=keyboard([])
        )
        return NAME_INPUT

    allowed = ["👧 Девочка", "👦 Мальчик"]

    if text not in allowed:
        await update.message.reply_text(
            "👶 Пожалуйста, выбери пол ребёнка кнопкой.",
            reply_markup=gender_keyboard()
        )
        return GENDER_INPUT

    context.user_data["gender"] = text

    await update.message.reply_text(
        "🎂 Теперь напиши возраст ребёнка.\n\n"
        "Примеры:\n"
        "2 — если ребёнку 2 года\n"
        "3,5 — если 3 года 5 месяцев\n"
        "3,10 — если 3 года 10 месяцев",
        reply_markup=keyboard([])
    )

    return AGE_INPUT


# =========================
# ВОЗРАСТ
# =========================

async def age_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_restart(text):
        return await start(update, context)

    if is_back(text):
        await update.message.reply_text(
            "👶 Вернёмся к выбору пола ребёнка.",
            reply_markup=gender_keyboard()
        )
        return GENDER_INPUT

    age = text.strip()
    ok, message = validate_age(age)

    if not ok:
        await update.message.reply_text(message, reply_markup=keyboard([]))
        return AGE_INPUT

    parsed = parse_age(age)

    context.user_data["pending_age"] = parsed["display"]
    context.user_data["pending_age_years"] = parsed["years"]
    context.user_data["pending_age_months"] = parsed["months"]

    await update.message.reply_text(
        f"🎂 Возраст ребёнка: {parsed['display']}\n\n"
        "Всё верно?",
        reply_markup=yes_change_keyboard()
    )

    return AGE_CONFIRM


async def age_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_restart(text):
        return await start(update, context)

    if is_back(text) or is_change(text):
        await update.message.reply_text(
            "🎂 Напиши возраст ребёнка заново.\n\n"
            "Например: 3,5",
            reply_markup=keyboard([])
        )
        return AGE_INPUT

    if not is_yes(text):
        await update.message.reply_text(
            "🌙 Нажми «✅ Всё верно» или «✏️ Изменить».",
            reply_markup=yes_change_keyboard()
        )
        return AGE_CONFIRM

    context.user_data["age"] = context.user_data["pending_age"]
    context.user_data["age_years"] = context.user_data["pending_age_years"]
    context.user_data["age_months"] = context.user_data["pending_age_months"]

    await update.message.reply_text(
        "🧸 Кого добавить в колыбельную?\n\n"
        "Это могут быть любимые герои, игрушки или близкие люди.\n\n"
        "Например:\n"
        "мама, папа, мишка, зайка, Синий трактор, Роналду\n\n"
        "Можно указать популярного героя или известного человека как добрый образ для мечты или игры.\n\n"
        "Если хочешь песню без персонажей — нажми кнопку ниже.",
        reply_markup=keyboard([
            ["🧸 Без персонажей"]
        ])
    )

    return CHAR_INPUT


# =========================
# ПЕРСОНАЖИ
# =========================

async def char_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_restart(text):
        return await start(update, context)

    if is_back(text):
        await update.message.reply_text(
            "🎂 Вернёмся к возрасту ребёнка.\n\n"
            "Напиши возраст числом. Например: 3,5",
            reply_markup=keyboard([])
        )
        return AGE_INPUT

    if text in ["🧸 Без персонажей", "без персонажей"]:
        characters = "без персонажей"
    else:
        characters = text.strip()

    ok, message = validate_common(characters)

    if not ok:
        await update.message.reply_text(message, reply_markup=keyboard([["🧸 Без персонажей"]]))
        return CHAR_INPUT

    safe_ok, safe_message = validate_child_safe_text(characters)

    if not safe_ok:
        await update.message.reply_text(safe_message, reply_markup=keyboard([["🧸 Без персонажей"]]))
        return CHAR_INPUT

    context.user_data["pending_characters"] = characters

    await update.message.reply_text(
        f"🧸 Персонажи и образы:\n{characters}\n\n"
        "Всё верно?",
        reply_markup=yes_change_keyboard()
    )

    return CHAR_CONFIRM


async def char_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_restart(text):
        return await start(update, context)

    if is_back(text) or is_change(text):
        await update.message.reply_text(
            "🧸 Напиши персонажей заново или нажми «🧸 Без персонажей».",
            reply_markup=keyboard([
                ["🧸 Без персонажей"]
            ])
        )
        return CHAR_INPUT

    if not is_yes(text):
        await update.message.reply_text(
            "🌙 Нажми «✅ Всё верно» или «✏️ Изменить».",
            reply_markup=yes_change_keyboard()
        )
        return CHAR_CONFIRM

    context.user_data["characters"] = context.user_data["pending_characters"]

    await update.message.reply_text(
        "🎤 Выбери голос будущей музыкальной колыбельной:",
        reply_markup=keyboard([
            ["👩 Женский голос"],
            ["👨 Мужской голос"],
            ["🧒 Детский голос"]
        ])
    )

    return VOICE


# =========================
# ГОЛОС
# =========================

async def voice_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_restart(text):
        return await start(update, context)

    if is_back(text):
        await update.message.reply_text(
            "🧸 Вернёмся к персонажам.\n\n"
            "Напиши персонажей заново или нажми «🧸 Без персонажей».",
            reply_markup=keyboard([
                ["🧸 Без персонажей"]
            ])
        )
        return CHAR_INPUT

    allowed = ["👩 Женский голос", "👨 Мужской голос", "🧒 Детский голос"]

    if text not in allowed:
        await update.message.reply_text(
            "🎤 Пожалуйста, выбери голос кнопкой.",
            reply_markup=keyboard([
                ["👩 Женский голос"],
                ["👨 Мужской голос"],
                ["🧒 Детский голос"]
            ])
        )
        return VOICE

    context.user_data["voice"] = text

    await update.message.reply_text(
        "✨ Какое настроение сделать у колыбельной?",
        reply_markup=keyboard([
            ["💗 Очень нежная"],
            ["🌟 Волшебная"],
            ["🌙 Спокойная"],
            ["🧚 Добрая сказочная"]
        ])
    )

    return MOOD


# =========================
# НАСТРОЕНИЕ
# =========================

async def mood_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_restart(text):
        return await start(update, context)

    if is_back(text):
        await update.message.reply_text(
            "🎤 Вернёмся к выбору голоса:",
            reply_markup=keyboard([
                ["👩 Женский голос"],
                ["👨 Мужской голос"],
                ["🧒 Детский голос"]
            ])
        )
        return VOICE

    allowed = ["💗 Очень нежная", "🌟 Волшебная", "🌙 Спокойная", "🧚 Добрая сказочная"]

    if text not in allowed:
        await update.message.reply_text(
            "✨ Пожалуйста, выбери настроение кнопкой.",
            reply_markup=keyboard([
                ["💗 Очень нежная"],
                ["🌟 Волшебная"],
                ["🌙 Спокойная"],
                ["🧚 Добрая сказочная"]
            ])
        )
        return MOOD

    context.user_data["mood"] = text

    await update.message.reply_text(
        "🌌 Выбери тему колыбельной:",
        reply_markup=keyboard([
            ["🌙 Звёзды и луна"],
            ["🌲 Лес и зверята"],
            ["☁️ Море и облака"],
            ["🧸 Игрушки засыпают"],
            ["🤍 Мама рядом"],
            ["💙 Папа рядом"],
            ["🌈 Свой вариант"]
        ])
    )

    return THEME


# =========================
# ТЕМА
# =========================

async def theme_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if is_restart(text):
        return await start(update, context)

    if is_back(text):
        await update.message.reply_text(
            "✨ Вернёмся к настроению колыбельной:",
            reply_markup=keyboard([
                ["💗 Очень нежная"],
                ["🌟 Волшебная"],
                ["🌙 Спокойная"],
                ["🧚 Добрая сказочная"]
            ])
        )
        return MOOD

    allowed = [
        "🌙 Звёзды и луна",
        "🌲 Лес и зверята",
        "☁️ Море и облака",
        "🧸 Игрушки засыпают",
        "🤍 Мама рядом",
        "💙 Папа рядом",
        "🌈 Свой вариант"
    ]

    if text not in allowed:
        await update.message.reply_text(
            "🌌 Пожалуйста, выбери тему кнопкой.",
            reply_markup=keyboard([
                ["🌙 Звёзды и луна"],
                ["🌲 Лес и зверята"],
                ["☁️ Море и облака"],
                ["🧸 Игрушки засыпают"],
                ["🤍 Мама рядом"],
                ["💙 Папа рядом"],
                ["🌈 Свой вариант"]
            ])
        )
        return THEME

    if text == "🌈 Свой вариант":
        await update.message.reply_text(
            "🌈 Напиши свою тему колыбельной.\n\n"
            "Тема должна быть мягкой, детской и спокойной.\n\n"
            "✨ Примеры:\n"
            "про космос и маленького робота\n"
            "про домик у озера\n"
            "про плюшевого мишку и звёзды",
            reply_markup=keyboard([])
        )
        return THEME_CUSTOM

    context.user_data["theme"] = text
    return await show_final_summary(update, context)


async def theme_custom_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if is_restart(text):
        return await start(update, context)

    if is_back(text):
        await update.message.reply_text(
            "🌌 Вернёмся к выбору темы:",
            reply_markup=keyboard([
                ["🌙 Звёзды и луна"],
                ["🌲 Лес и зверята"],
                ["☁️ Море и облака"],
                ["🧸 Игрушки засыпают"],
                ["🤍 Мама рядом"],
                ["💙 Папа рядом"],
                ["🌈 Свой вариант"]
            ])
        )
        return THEME

    ok, message = validate_common(text)

    if not ok:
        await update.message.reply_text(message, reply_markup=keyboard([]))
        return THEME_CUSTOM

    safe_ok, safe_message = validate_child_safe_text(text)

    if not safe_ok:
        await update.message.reply_text(safe_message, reply_markup=keyboard([]))
        return THEME_CUSTOM

    if len(text) > 100:
        await update.message.reply_text(
            "✨ Тема получилась длинной. Напиши чуть короче, чтобы песня была лёгкой.",
            reply_markup=keyboard([])
        )
        return THEME_CUSTOM

    context.user_data["theme"] = text
    return await show_final_summary(update, context)


# =========================
# ФИНАЛЬНАЯ ПРОВЕРКА
# =========================

async def show_final_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data

    summary = f"""
🌙 Почти готово! Проверь данные:

👶 Имя: {data["name"]}
🎵 Произношение для песни: {data["name_stressed"]}
👶 Пол: {data["gender"]}
🎂 Возраст: {data["age"]}
🧸 Персонажи: {data["characters"]}
🎤 Голос: {data["voice"]}
✨ Настроение: {data["mood"]}
🌌 Тема: {data["theme"]}

Создаём текст колыбельной?
"""

    await update.message.reply_text(
        summary,
        reply_markup=keyboard([
            ["✨ Создать текст"],
            ["✏️ Изменить данные"]
        ])
    )

    return FINAL_CONFIRM


async def final_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_restart(text):
        return await start(update, context)

    if is_back(text) or text in ["✏️ Изменить данные", "Изменить данные"]:
        await update.message.reply_text(
            "🌌 Хорошо, вернёмся к теме колыбельной:",
            reply_markup=keyboard([
                ["🌙 Звёзды и луна"],
                ["🌲 Лес и зверята"],
                ["☁️ Море и облака"],
                ["🧸 Игрушки засыпают"],
                ["🤍 Мама рядом"],
                ["💙 Папа рядом"],
                ["🌈 Свой вариант"]
            ])
        )
        return THEME

    if text not in ["✨ Создать текст", "Создать текст"]:
        await update.message.reply_text(
            "🌙 Нажми «✨ Создать текст» или «✏️ Изменить данные».",
            reply_markup=keyboard([
                ["✨ Создать текст"],
                ["✏️ Изменить данные"]
            ])
        )
        return FINAL_CONFIRM

    user_id = update.effective_user.id

    if get_nuts(user_id) < NUTS_PER_GENERATION:
        await offer_buy_nuts(update, user_id)
        return START

    await update.message.reply_text(
        f"✨ {BRAND_NAME} создаёт текст колыбельной и расставляет правильные ударения 🌙\n\n"
        f"Обычно это занимает 1-2 минуты.",
        reply_markup=generation_wait_keyboard()
    )

    try:
        lullaby_text, safety_note = await run_with_wait_messages(
            update,
            generate_and_prepare_lullaby,
            context.user_data,
            wait_messages=CREATE_TEXT_WAIT_MESSAGES,
            max_seconds=TEXT_GENERATION_TIMEOUT_SECONDS,
        )

        context.user_data["lullaby_text"] = lullaby_text
        context.user_data["edit_count"] = 0

        await update.message.reply_text("🌙 Текст колыбельной готов:")
        await send_long_text(update, lullaby_text)

        await update.message.reply_text(
            "✨ Текст тебе нравится?",
            reply_markup=text_review_keyboard()
        )

        return LULLABY_REVIEW

    except asyncio.TimeoutError:
        await update.message.reply_text(
            "😔 Текст создаётся слишком долго, похоже связь с сервисом оборвалась.\n\n"
            "🌰 Орешек не списан.\n\n"
            "Можно попробовать создать текст ещё раз или изменить данные.",
            reply_markup=keyboard([
                ["✨ Создать текст"],
                ["✏️ Изменить данные"]
            ])
        )
        return FINAL_CONFIRM

    except Exception as error:
        print("Ошибка OpenAI:", error)

        await update.message.reply_text(
            "😔 Не получилось создать текст колыбельной.\n\n"
            "🌰 Орешек не списан.\n\n"
            "Иногда сервис отвечает слишком долго. Можно попробовать ещё раз.",
            reply_markup=keyboard([
                ["✨ Создать текст"],
                ["✏️ Изменить данные"]
            ])
        )
        return FINAL_CONFIRM


# =========================
# ПРОВЕРКА / РЕДАКТИРОВАНИЕ ТЕКСТА
# =========================

async def lullaby_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_restart(text):
        return await start(update, context)

    if is_back(text):
        return await show_final_summary(update, context)

    if text in ["✅ Подтвердить", "Подтвердить"]:
        if not context.user_data.get("lullaby_text"):
            await update.message.reply_text(
                "🌙 Я не вижу готовый текст колыбельной в текущем сеансе.\n\n"
                "Похоже, бот перезапускался или потерял временное состояние. "
                "Давай начнём создание заново, чтобы музыка собрала именно твой текст.",
                reply_markup=main_menu_keyboard()
            )
            return START

        await update.message.reply_text(
            "✅ Текст подтверждён.\n\n"
            "Теперь можно создать музыкальную колыбельную 🎵",
            reply_markup=create_music_keyboard()
        )
        return GENERATE_MUSIC

    if text in ["✏️ Редактировать", "Редактировать"]:
        edit_count = context.user_data.get("edit_count", 0)

        if edit_count >= MAX_EDITS:
            await update.message.reply_text(
                "🌙 Лимит правок уже использован.\n\n"
                "Теперь можно создать музыкальную версию из текущего текста 🎵",
                reply_markup=create_music_keyboard()
            )
            return GENERATE_MUSIC

        left = MAX_EDITS - edit_count

        await update.message.reply_text(
            f"✏️ Напиши, что изменить в тексте.\n\n"
            f"Примеры:\n"
            f"«Сделай припев нежнее»\n"
            f"«Добавь больше звёзд и луны»\n"
            f"«Убери мишку из второго куплета»\n\n"
            f"Осталось правок: {left}",
            reply_markup=keyboard([])
        )

        return EDIT_REQUEST

    await update.message.reply_text(
        "🌙 Нажми «✅ Подтвердить» или «✏️ Редактировать».",
        reply_markup=text_review_keyboard()
    )
    return LULLABY_REVIEW


async def edit_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if is_restart(text):
        return await start(update, context)

    if is_back(text):
        await update.message.reply_text(
            "✨ Текст тебе нравится?",
            reply_markup=text_review_keyboard()
        )
        return LULLABY_REVIEW

    ok, message = validate_common(text)

    if not ok:
        await update.message.reply_text(message, reply_markup=keyboard([]))
        return EDIT_REQUEST

    safe_ok, safe_message = validate_child_safe_text(text)

    if not safe_ok:
        await update.message.reply_text(safe_message, reply_markup=keyboard([]))
        return EDIT_REQUEST

    edit_count = context.user_data.get("edit_count", 0)

    if edit_count >= MAX_EDITS:
        await update.message.reply_text(
            "🌙 Лимит правок уже использован.\n\n"
            "Теперь можно создать музыкальную колыбельную 🎵",
            reply_markup=create_music_keyboard()
        )
        return GENERATE_MUSIC

    await update.message.reply_text(
        f"✨ {BRAND_NAME} аккуратно вносит правки...\n\n"
        f"Сохраняю нежность, ритм и правильные ударения 🌙",
        reply_markup=generation_wait_keyboard()
    )

    try:
        new_text, safety_note = await run_with_wait_messages(
            update,
            edit_and_prepare_lullaby,
            context.user_data,
            context.user_data["lullaby_text"],
            text,
            wait_messages=EDIT_TEXT_WAIT_MESSAGES,
            max_seconds=TEXT_GENERATION_TIMEOUT_SECONDS,
        )

        context.user_data["lullaby_text"] = new_text
        context.user_data["edit_count"] = edit_count + 1

        await update.message.reply_text("🌙 Обновлённый текст:")
        await send_long_text(update, new_text)

        if context.user_data["edit_count"] >= MAX_EDITS:
            await update.message.reply_text(
                "✨ Лимит правок использован.\n\n"
                "Теперь можно создать музыкальную колыбельную 🎵",
                reply_markup=create_music_keyboard()
            )
            return GENERATE_MUSIC

        left = MAX_EDITS - context.user_data["edit_count"]

        await update.message.reply_text(
            f"✨ Теперь текст подходит?\n\n"
            f"Осталось правок: {left}",
            reply_markup=text_review_keyboard()
        )

        return LULLABY_REVIEW

    except asyncio.TimeoutError:
        await update.message.reply_text(
            "😔 Правка текста заняла слишком много времени.\n\n"
            "🌰 Орешек не списан.\n\n"
            "Попробуй написать правку чуть проще или вернись к текущему тексту.",
            reply_markup=text_review_keyboard()
        )
        return LULLABY_REVIEW

    except Exception as error:
        print("Ошибка OpenAI:", error)
        await update.message.reply_text(
            "😔 Не получилось изменить текст.\n\n"
            "🌰 Орешек не списан.\n\n"
            "Попробуй написать правку чуть проще.",
            reply_markup=keyboard([])
        )
        return EDIT_REQUEST


# =========================
# СОЗДАНИЕ МУЗЫКИ
# =========================

async def generate_music_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_restart(text):
        return await start(update, context)

    if is_back(text):
        await update.message.reply_text(
            "✨ Текст тебе нравится?",
            reply_markup=text_review_keyboard()
        )
        return LULLABY_REVIEW

    if text not in ["🎵 Создать музыку", "Создать музыку"]:
        await update.message.reply_text(
            "🎵 Нажми кнопку «🎵 Создать музыку».",
            reply_markup=create_music_keyboard()
        )
        return GENERATE_MUSIC

    return await generate_music(update, context)


async def generate_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = context.user_data

    if get_nuts(user_id) < NUTS_PER_GENERATION:
        await update.message.reply_text(
            "🌙 Для создания музыки нужен 1 орешек.",
            reply_markup=buy_keyboard()
        )
        return START

    lullaby_text = data.get("lullaby_text")

    if not lullaby_text:
        await update.message.reply_text(
            "🌙 Не нашла готовый текст колыбельной.\n\n"
            "Похоже, бот перезапускался или старый шаг уже устарел. "
            "Создай текст заново, и я сразу соберу из него музыку.",
            reply_markup=main_menu_keyboard()
        )
        return START

    await update.message.reply_text(
        f"🎵 {BRAND_NAME} создаёт музыкальную колыбельную...\n\n"
        f"Сейчас текст превратится в нежную песню для сна 🌙\n\n"
        f"Обычно это занимает 3-5 минут.",
        reply_markup=generation_wait_keyboard()
    )

    try:
        started_at = time.monotonic()
        genitive_name = make_genitive_name(data["name"])
        title = f"Колыбельная для {genitive_name}"
        style = make_music_style(data)

        task_id = await asyncio.wait_for(
            asyncio.to_thread(create_music_task, lullaby_text, style, title),
            timeout=max(1, seconds_left(started_at, MUSIC_GENERATION_TIMEOUT_SECONDS)),
        )

        audio_urls = []
        sent_messages = set()

        while seconds_left(started_at, MUSIC_GENERATION_TIMEOUT_SECONDS) > 0:
            await asyncio.sleep(
                min(10, max(1, seconds_left(started_at, MUSIC_GENERATION_TIMEOUT_SECONDS)))
            )

            audio_urls = await asyncio.wait_for(
                asyncio.to_thread(get_music_audio_urls, task_id),
                timeout=max(1, min(60, seconds_left(started_at, MUSIC_GENERATION_TIMEOUT_SECONDS))),
            )

            if audio_urls:
                break

            elapsed = time.monotonic() - started_at

            if elapsed >= 70 and "soon" not in sent_messages:
                sent_messages.add("soon")
                await update.message.reply_text(
                    "⏳ Музыка ещё создаётся... Уже скоро будет волшебство 🌙",
                    reply_markup=generation_wait_keyboard()
                )

            if elapsed >= 150 and "almost" not in sent_messages:
                sent_messages.add("almost")
                await update.message.reply_text(
                    "🎵 Почти готово. Собираю музыкальную колыбельную...",
                    reply_markup=generation_wait_keyboard()
                )

            if elapsed >= 420 and "long" not in sent_messages:
                sent_messages.add("long")
                await update.message.reply_text(
                    "🌙 Музыка создаётся дольше обычного, я всё ещё проверяю результат.\n\n"
                    "Орешек спишется только после отправки готовой песни.",
                    reply_markup=generation_wait_keyboard()
                )

        if not audio_urls:
            await update.message.reply_text(
                "😔 Музыкальная колыбельная пока не готова.\n\n"
                "🌰 Орешек не списан.\n\n"
                "Можно попробовать создать музыку ещё раз позже.",
                reply_markup=create_music_keyboard()
            )
            return GENERATE_MUSIC

        safe_title = clean_filename(title)
        audio_url = audio_urls[0]

        await update.message.reply_text(
            "✨ Готово! Колыбельная создана 🎵",
            reply_markup=generation_wait_keyboard()
        )

        filename = f"{safe_title}.mp3"
        file_path = await asyncio.wait_for(
            asyncio.to_thread(download_audio, audio_url, filename),
            timeout=max(1, seconds_left(started_at, MUSIC_GENERATION_TIMEOUT_SECONDS)),
        )

        with open(file_path, "rb") as audio_file:
            await update.message.reply_document(
                document=audio_file,
                filename=filename,
                caption=f"🌙 {title}",
                read_timeout=180,
                write_timeout=180,
                connect_timeout=60,
                pool_timeout=180
            )

        os.remove(file_path)

        nuts_removed = remove_nuts(user_id, NUTS_PER_GENERATION)
        balance = get_nuts(user_id)
        mark_lullaby_created(user_id)
        add_lullabies(user_id, 1)

        context.user_data.clear()

        if not nuts_removed:
            await update.message.reply_text(
                "⚠️ Колыбельная отправлена, но я не смогла списать орешек с баланса.\n\n"
                "Пожалуйста, напиши поддержке, чтобы мы проверили личный кабинет.",
                reply_markup=main_menu_keyboard()
            )
            return START

        await update.message.reply_text(
            f"🌙 Всё готово!\n\n"
            f"Спасибо, что создали колыбельную в {BRAND_NAME} ✨\n\n"
            f"🌰 Списан 1 орешек\n"
            f"Баланс: {balance} орешков\n\n"
            f"Можно создать новую колыбельную или зайти в личный кабинет.",
            reply_markup=main_menu_keyboard()
        )

        return START

    except asyncio.TimeoutError:
        print("Suno/музыка: превышено время ожидания")

        await update.message.reply_text(
            "😔 Музыкальная колыбельная создаётся слишком долго, похоже связь с сервисом оборвалась.\n\n"
            "🌰 Орешек не списан.\n\n"
            "Можно попробовать создать музыку ещё раз позже.",
            reply_markup=create_music_keyboard()
        )
        return GENERATE_MUSIC

    except Exception as error:
        print("Ошибка Suno/музыки:", error)

        await update.message.reply_text(
            "😔 Не получилось создать музыкальную колыбельную.\n\n"
            "🌰 Орешек не списан.\n\n"
            "Можно попробовать ещё раз позже.",
            reply_markup=create_music_keyboard()
        )
        return GENERATE_MUSIC


# =========================
# ОТМЕНА / ЗАПУСК
# =========================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    await update.message.reply_text(
        "🌙 Создание колыбельной отменено.\n\n"
        "Чтобы начать заново, нажми кнопку ниже.",
        reply_markup=main_menu_keyboard()
    )

    return START


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    create_user_if_not_exists(update.effective_user)
    nuts = get_nuts(update.effective_user.id)

    await update.message.reply_text(
        f"🌰 Твой баланс: {nuts} орешков\n\n"
        f"1 орешек = 1 персональная музыкальная колыбельная",
        reply_markup=profile_keyboard()
    )


def get_command_payload(update: Update):
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


REMINDER_MESSAGE_VARIANTS = [
    (
        "🌙 Иногда новая колыбельная нужна не потому, что старая надоела.\n\n"
        "Её можно сделать под событие: новый велосипед, поездку, день рождения, "
        "первый день в школе или просто важный вечер.\n\n"
        "Получится песня, в которой ребёнку мягко снится именно его сегодняшний маленький праздник."
    ),
    (
        "✨ У ребёнка появился новый этап?\n\n"
        "Пошёл в школу, родился братик или сестрёнка, появилась любимая игрушка, "
        "впереди поездка или большое семейное событие.\n\n"
        "Можно создать колыбельную про этот момент, чтобы день закончился спокойно и тепло."
    ),
    (
        "🌙 Колыбельная может быть тёплым завершением дня.\n\n"
        "Сегодня гуляли, катались, учились чему-то новому или просто был хороший вечер дома? "
        "Из этого можно сделать маленькую песню для сна.\n\n"
        "Так у ребёнка появляется не одна колыбельная навсегда, а нежная музыкальная память о разных днях."
    ),
    (
        "💛 Иногда самые важные истории совсем простые.\n\n"
        "Новая игрушка, первый велосипед, дорога в гости, праздник, встреча с родными "
        "или день, когда ребёнок особенно старался.\n\n"
        "Можно превратить это в персональную колыбельную, чтобы ребёнок засыпал с добрым образом в голове."
    ),
]


def reminder_message(user_id=None):
    day_number = int(time.time() // 86400)

    if user_id is None:
        index = day_number % len(REMINDER_MESSAGE_VARIANTS)
    else:
        index = (day_number + int(user_id)) % len(REMINDER_MESSAGE_VARIANTS)

    return (
        REMINDER_MESSAGE_VARIANTS[index] +
        "\n\nСоздать новую колыбельную можно в главном меню 🌙\n"
        "Если напоминания не нужны, их можно отключить командой /stopreminders."
    )


async def send_message_safely(bot, user_id, text, reply_markup=None):
    try:
        await bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=reply_markup,
        )
        return True
    except Exception as error:
        print(f"Не удалось отправить сообщение пользователю {user_id}:", error)
        return False


async def send_bulk_message(bot, user_ids, text, reply_markup=None, mark_reminders=False):
    sent = 0
    failed = 0

    for user_id in user_ids:
        message_text = text(user_id) if callable(text) else text
        ok = await send_message_safely(bot, user_id, message_text, reply_markup=reply_markup)

        if ok:
            sent += 1
            if mark_reminders:
                mark_reminder_sent(user_id)
        else:
            failed += 1

        await asyncio.sleep(0.05)

    return sent, failed


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🌙 Эта команда доступна только администратору.")
        return

    message = get_command_payload(update)

    if not message:
        await update.message.reply_text(
            "📣 Формат команды:\n"
            "/broadcast текст рассылки\n\n"
            "Например:\n"
            "/broadcast Сегодня вечером будет технический перерыв."
        )
        return

    user_ids = get_all_user_ids()

    await update.message.reply_text(
        f"📣 Начинаю рассылку для пользователей: {len(user_ids)}"
    )

    sent, failed = await send_bulk_message(
        context.bot,
        user_ids,
        message,
        reply_markup=main_menu_keyboard(),
    )

    await update.message.reply_text(
        "✅ Рассылка завершена.\n\n"
        f"Отправлено: {sent}\n"
        f"Ошибок: {failed}"
    )


async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🌙 Эта команда доступна только администратору.")
        return

    details = get_command_payload(update)
    message = (
        "🛠 Технический перерыв\n\n"
        "Скоро бот может ненадолго работать нестабильно: мы обновляем Колыбелку, "
        "чтобы оплата, орешки и создание песен работали спокойнее."
    )

    if details:
        message += f"\n\n{details}"

    user_ids = get_all_user_ids()
    await update.message.reply_text(
        f"🛠 Отправляю уведомление о техперерыве: {len(user_ids)} пользователей"
    )

    sent, failed = await send_bulk_message(
        context.bot,
        user_ids,
        message,
        reply_markup=main_menu_keyboard(),
    )

    await update.message.reply_text(
        "✅ Уведомление отправлено.\n\n"
        f"Отправлено: {sent}\n"
        f"Ошибок: {failed}"
    )


async def remindnow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🌙 Эта команда доступна только администратору.")
        return

    user_ids = get_users_for_reminder()

    await update.message.reply_text(
        f"🌙 Запускаю мягкое напоминание: {len(user_ids)} пользователей"
    )

    sent, failed = await send_bulk_message(
        context.bot,
        user_ids,
        reminder_message,
        reply_markup=main_menu_keyboard(),
        mark_reminders=True,
    )

    await update.message.reply_text(
        "✅ Напоминания отправлены.\n\n"
        f"Отправлено: {sent}\n"
        f"Ошибок: {failed}"
    )


async def remindpreview_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🌙 Эта команда доступна только администратору.")
        return

    await update.message.reply_text(
        "🌙 Варианты мягких напоминаний:"
    )

    for index, message in enumerate(REMINDER_MESSAGE_VARIANTS, start=1):
        await update.message.reply_text(
            f"Вариант {index}\n\n{message}\n\n"
            "Создать новую колыбельную можно в главном меню 🌙\n"
            "Если напоминания не нужны, их можно отключить командой /stopreminders."
        )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🌙 Эта команда доступна только администратору.")
        return

    all_user_ids = get_all_user_ids()
    reminder_user_ids = get_users_for_reminder()
    user_summaries = get_user_summaries()
    legacy_db_path = os.path.join(BASE_DIR, "kolybelka.db")
    legacy_users_count = 0

    if os.path.abspath(DB_PATH) != os.path.abspath(legacy_db_path):
        legacy_users_count = len(read_legacy_db_rows(legacy_db_path, "users"))

    if user_summaries:
        users_preview = "\n".join(
            f"• {user['user_id']}"
            f"{' @' + user['username'] if user['username'] else ''}"
            f" | 🌰 {user['nuts']} | 🎵 {user['lullabies']}"
            for user in user_summaries
        )
    else:
        users_preview = "пока пусто"

    empty_note = ""

    if not all_user_ids:
        empty_note = (
            "\n\nЕсли в текущей базе 0 пользователей, рассылка технически работает, "
            "но отправлять её некому."
        )

    await update.message.reply_text(
        "👥 Пользователи бота\n\n"
        f"Всего в базе: {len(all_user_ids)}\n"
        f"Подходят для мягкого напоминания сейчас: {len(reminder_user_ids)}\n\n"
        f"Текущая SQLite база: {DB_PATH}\n"
        f"Пользователей в старой базе рядом с bot.py: {legacy_users_count}\n\n"
        f"Последние пользователи:\n{users_preview}"
        f"{empty_note}"
    )


async def dbstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🌙 Эта команда доступна только администратору.")
        return

    stats = get_database_stats()
    legacy_db_path = os.path.join(BASE_DIR, "kolybelka.db")
    legacy_users_count = 0

    if os.path.abspath(DB_PATH) != os.path.abspath(legacy_db_path):
        legacy_users_count = len(read_legacy_db_rows(legacy_db_path, "users"))

    await update.message.reply_text(
        "🗄 SQLite база\n\n"
        f"Текущая база: {stats['db_path']}\n"
        f"Файл существует: {'да' if stats['db_exists'] else 'нет'}\n"
        f"Размер файла: {stats['db_size']} байт\n"
        f"Папка проекта: {stats['base_dir']}\n"
        f"SHARED_DIR: {stats['shared_dir'] or 'не задан'}\n"
        f"DB_PATH задан явно: {'да' if stats['db_path_configured'] else 'нет'}\n"
        f"База внутри папки проекта: {'да' if stats['db_path_in_project_dir'] else 'нет'}\n"
        f"База в /app/shared: {'да' if stats['db_path_in_shared_dir'] else 'нет'}\n"
        f"Файл состояния: {stats['persistence_path']}\n\n"
        f"Пользователей: {stats['users_count']}\n"
        f"Платежей: {stats['payments_count']}\n"
        f"Орешков всего на балансах: {stats['total_nuts']}\n"
        f"Пользователей в старой базе рядом с bot.py: {legacy_users_count}\n\n"
        f"{stats['db_persistence_warning']}"
    )


async def storagecheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🌙 Эта команда доступна только администратору.")
        return

    stats = get_database_stats()
    probe = get_or_create_storage_probe()

    if probe["file_error"]:
        file_status = f"ошибка записи/чтения: {probe['file_error']}"
    elif probe["tokens_match"]:
        file_status = "файл проверки совпадает с SQLite"
    else:
        file_status = "⚠️ файл проверки не совпадает с SQLite"

    await update.message.reply_text(
        "🧪 Проверка постоянного хранилища\n\n"
        f"SQLite база: {stats['db_path']}\n"
        f"Файл проверки: {probe['probe_file_path']}\n"
        f"Токен SQLite: {probe['db_token']}\n"
        f"Токен файла: {probe['file_token'] or 'нет'}\n"
        f"Создано в SQLite: {probe['db_created_at']}\n"
        f"Статус файла: {file_status}\n\n"
        f"{stats['db_persistence_warning']}\n\n"
        "Как проверить: запусти /storagecheck, потом обнови бота с Git и перезапусти. "
        "После этого снова запусти /storagecheck. Если токен изменился или файл пропал, "
        "хранилище не постоянное, и орешки будут сбрасываться."
    )


async def stopreminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    create_user_if_not_exists(update.effective_user)
    set_reminders_enabled(update.effective_user.id, False)

    await update.message.reply_text(
        "🌙 Хорошо, я больше не буду присылать напоминания.\n\n"
        "Основные сообщения бота и оплата продолжат работать.",
        reply_markup=main_menu_keyboard()
    )


async def startreminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    create_user_if_not_exists(update.effective_user)
    set_reminders_enabled(update.effective_user.id, True)

    await update.message.reply_text(
        "🌙 Готово, мягкие напоминания снова включены.",
        reply_markup=main_menu_keyboard()
    )


async def track_user_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        create_user_if_not_exists(update.effective_user)


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username or "без username"

    await update.message.reply_text(
        f"👤 Твой Telegram ID:\n{update.effective_user.id}\n\n"
        f"Username: @{username}"
    )


def is_admin(user_id):
    return user_id in ADMIN_IDS


async def addnuts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🌙 Эта команда доступна только администратору.")
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "🌰 Формат команды:\n"
            "/addnuts user_id количество\n\n"
            "Например:\n"
            "/addnuts 123456789 1"
        )
        return

    try:
        target_user_id = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("🌙 user_id и количество должны быть числами.")
        return

    if amount <= 0 or amount > 100:
        await update.message.reply_text("🌙 Количество орешков должно быть от 1 до 100.")
        return

    create_user_id_if_not_exists(target_user_id)
    add_nuts(target_user_id, amount)
    balance = get_nuts(target_user_id)

    await update.message.reply_text(
        "✅ Орешки начислены вручную.\n\n"
        f"Пользователь: {target_user_id}\n"
        f"Начислено: {amount}\n"
        f"Баланс: {balance}"
    )

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=(
                "✅ Орешки начислены!\n\n"
                f"🌰 Начислено: {amount} орешков\n"
                f"Текущий баланс: {balance} орешков"
            ),
            reply_markup=profile_keyboard()
        )
    except Exception as error:
        print("Не удалось уведомить пользователя о ручном начислении:", error)


async def removenuts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🌙 Эта команда доступна только администратору.")
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "🌰 Формат команды:\n"
            "/removenuts user_id количество\n\n"
            "Например:\n"
            "/removenuts 123456789 1"
        )
        return

    try:
        target_user_id = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("🌙 user_id и количество должны быть числами.")
        return

    if amount <= 0 or amount > 100:
        await update.message.reply_text("🌙 Количество орешков должно быть от 1 до 100.")
        return

    create_user_id_if_not_exists(target_user_id)
    balance_before = get_nuts(target_user_id)

    if balance_before < amount:
        await update.message.reply_text(
            "🌙 Не получилось списать орешки вручную.\n\n"
            f"Пользователь: {target_user_id}\n"
            f"Текущий баланс: {balance_before}\n"
            f"Нужно списать: {amount}"
        )
        return

    remove_nuts(target_user_id, amount)
    balance = get_nuts(target_user_id)

    await update.message.reply_text(
        "✅ Орешки списаны вручную.\n\n"
        f"Пользователь: {target_user_id}\n"
        f"Списано: {amount}\n"
        f"Баланс: {balance}"
    )

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=(
                "🌰 Баланс орешков обновлён администратором.\n\n"
                f"Списано: {amount}\n"
                f"Текущий баланс: {balance} орешков"
            ),
            reply_markup=profile_keyboard()
        )
    except Exception as error:
        print("Не удалось уведомить пользователя о ручном списании:", error)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("⚠️ Ошибка Telegram/сети:", context.error)


async def send_automatic_reminders(app):
    if not REMINDERS_ENABLED:
        return

    user_ids = get_users_for_reminder()

    if not user_ids:
        return

    print(f"Автонапоминания: найдено пользователей {len(user_ids)}")

    sent, failed = await send_bulk_message(
        app.bot,
        user_ids,
        reminder_message,
        reply_markup=main_menu_keyboard(),
        mark_reminders=True,
    )

    print(f"Автонапоминания: отправлено {sent}, ошибок {failed}")


async def reminder_worker(app):
    await asyncio.sleep(120)

    while True:
        try:
            await send_automatic_reminders(app)
        except Exception as error:
            print("Ошибка автоматических напоминаний:", error)

        await asyncio.sleep(max(3600, REMINDER_INTERVAL_HOURS * 3600))


async def on_app_start(app):
    global TELEGRAM_EVENT_LOOP, REMINDER_TASK
    TELEGRAM_EVENT_LOOP = asyncio.get_running_loop()

    if REMINDER_TASK is None or REMINDER_TASK.done():
        REMINDER_TASK = asyncio.create_task(reminder_worker(app))


def main():
    migrate_existing_db_to_persistent_path()
    ensure_persistence_dir()
    init_db()
    merge_legacy_db_into_current_db()
    init_db()
    print(f"SQLite DB_PATH: {DB_PATH}")
    print(f"Telegram state PERSISTENCE_PATH: {PERSISTENCE_PATH}")
    print(get_db_persistence_warning())

    if not TELEGRAM_TOKEN:
        print("Ошибка: TELEGRAM_TOKEN не найден")
        return

    if not OPENAI_API_KEY:
        print("Ошибка: OPENAI_API_KEY не найден")
        return

    if not SUNO_API_KEY:
        print("Ошибка: SUNO_API_KEY не найден")
        return

    if not yookassa_is_configured():
        print("ЮKassa не настроена: бот запустится, но оплата будет временно недоступна")
    elif YOOKASSA_TEST_MODE:
        print("ЮKassa работает в тестовом режиме")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .persistence(PicklePersistence(filepath=PERSISTENCE_PATH))
        .post_init(on_app_start)
        .connect_timeout(60)
        .read_timeout(180)
        .write_timeout(180)
        .pool_timeout(180)
        .build()
    )

    global_button_texts = [
        "🏠 Главное меню",
        "Главное меню",
        "🌰 Купить орешки",
        "👤 Личный кабинет",
        "🌙 Создать новую колыбельную",
        "Создать новую колыбельную",
        "🌙 Создать колыбельную",
        "Создать колыбельную",
        *NUT_PACKAGES.keys(),
    ]
    global_button_filter = filters.Regex(
        "^(" + "|".join(re.escape(text) for text in global_button_texts) + ")$"
    )

    def global_button_handler(source_state):
        async def handle_global_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
            return await global_button(update, context, source_state)

        return MessageHandler(global_button_filter, handle_global_button)

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^🔄 Начать заново$"), start),
            MessageHandler(filters.Regex("^🔄 Начать сначала$"), start),
            MessageHandler(filters.Regex("^Начать заново$"), start),
            MessageHandler(filters.Regex("^Начать сначала$"), start),
        ],
        states={
            START: [MessageHandler(filters.TEXT & ~filters.COMMAND, start_button)],
            NAME_INPUT: [global_button_handler(NAME_INPUT), MessageHandler(filters.TEXT & ~filters.COMMAND, name_input)],
            NAME_CONFIRM: [global_button_handler(NAME_CONFIRM), MessageHandler(filters.TEXT & ~filters.COMMAND, name_confirm)],
            GENDER_INPUT: [global_button_handler(GENDER_INPUT), MessageHandler(filters.TEXT & ~filters.COMMAND, gender_input)],
            AGE_INPUT: [global_button_handler(AGE_INPUT), MessageHandler(filters.TEXT & ~filters.COMMAND, age_input)],
            AGE_CONFIRM: [global_button_handler(AGE_CONFIRM), MessageHandler(filters.TEXT & ~filters.COMMAND, age_confirm)],
            CHAR_INPUT: [global_button_handler(CHAR_INPUT), MessageHandler(filters.TEXT & ~filters.COMMAND, char_input)],
            CHAR_CONFIRM: [global_button_handler(CHAR_CONFIRM), MessageHandler(filters.TEXT & ~filters.COMMAND, char_confirm)],
            VOICE: [global_button_handler(VOICE), MessageHandler(filters.TEXT & ~filters.COMMAND, voice_choice)],
            MOOD: [global_button_handler(MOOD), MessageHandler(filters.TEXT & ~filters.COMMAND, mood_choice)],
            THEME: [global_button_handler(THEME), MessageHandler(filters.TEXT & ~filters.COMMAND, theme_choice)],
            THEME_CUSTOM: [global_button_handler(THEME_CUSTOM), MessageHandler(filters.TEXT & ~filters.COMMAND, theme_custom_input)],
            FINAL_CONFIRM: [global_button_handler(FINAL_CONFIRM), MessageHandler(filters.TEXT & ~filters.COMMAND, final_confirm)],
            LULLABY_REVIEW: [global_button_handler(LULLABY_REVIEW), MessageHandler(filters.TEXT & ~filters.COMMAND, lullaby_review)],
            EDIT_REQUEST: [global_button_handler(EDIT_REQUEST), MessageHandler(filters.TEXT & ~filters.COMMAND, edit_request)],
            GENERATE_MUSIC: [global_button_handler(GENERATE_MUSIC), MessageHandler(filters.TEXT & ~filters.COMMAND, generate_music_button)],
            PAYMENT_EMAIL_INPUT: [global_button_handler(PAYMENT_EMAIL_INPUT), MessageHandler(filters.TEXT & ~filters.COMMAND, payment_email_input)],
            PROFILE_VIEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, profile_view)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^🔄 Начать заново$"), start),
            MessageHandler(filters.Regex("^🔄 Начать сначала$"), start),
            MessageHandler(filters.Regex("^Начать заново$"), start),
            MessageHandler(filters.Regex("^Начать сначала$"), start),
        ],
        name="lullaby_conversation",
        persistent=True,
        allow_reentry=True,
    )

    app.add_handler(MessageHandler(filters.ALL, track_user_activity), group=-2)
    app.add_handler(conversation)
    app.add_handler(CommandHandler("balance", balance_command), group=-1)
    app.add_handler(CommandHandler("myid", myid_command), group=-1)
    app.add_handler(CommandHandler("broadcast", broadcast_command), group=-1)
    app.add_handler(CommandHandler("maintenance", maintenance_command), group=-1)
    app.add_handler(CommandHandler("remindnow", remindnow_command), group=-1)
    app.add_handler(CommandHandler("remindpreview", remindpreview_command), group=-1)
    app.add_handler(CommandHandler("users", users_command), group=-1)
    app.add_handler(CommandHandler("dbstatus", dbstatus_command), group=-1)
    app.add_handler(CommandHandler("storagecheck", storagecheck_command), group=-1)
    app.add_handler(CommandHandler("stopreminders", stopreminders_command), group=-1)
    app.add_handler(CommandHandler("startreminders", startreminders_command), group=-1)
    app.add_handler(CommandHandler("addnuts", addnuts_command), group=-1)
    app.add_handler(CommandHandler("removenuts", removenuts_command), group=-1)
    app.add_error_handler(error_handler)

    if yookassa_is_configured():
        start_yookassa_webhook_server(app)

    print("Колыбелка запущена...")
    app.run_polling()


if __name__ == "__main__":
    main()
