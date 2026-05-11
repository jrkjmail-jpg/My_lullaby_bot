import os
import re
import uuid
import sqlite3
import asyncio
import tempfile
import requests
from dotenv import load_dotenv
from openai import OpenAI

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
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

OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY)
SUNO_BASE_URL = "https://api.sunoapi.org"
YOOKASSA_BASE_URL = "https://api.yookassa.ru/v3"

DB_PATH = "kolybelka.db"

MAX_EDITS = 5
LULLABIES_PER_GENERATION = 1

LULLABY_PACKAGES = {
    "🌙 Купить 1 колыбельную": {
        "lullabies": 1,
        "price": "350.00",
        "title": "1 персональная музыкальная колыбельная",
    },
    "🌙 Купить 2 колыбельные": {
        "lullabies": 2,
        "price": "500.00",
        "title": "2 персональные музыкальные колыбельные",
    },
    "🌙 Купить 3 колыбельные": {
        "lullabies": 3,
        "price": "600.00",
        "title": "3 персональные музыкальные колыбельные",
    },
}

PAYMENT_CHECK_BUTTON = "✅ Проверить оплату"

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
) = range(17)


BAD_WORDS = [
    "дурак", "дура", "идиот", "идиотка", "тупой", "тупая",
    "блять", "блядь", "сука", "хуй", "пизда", "ебать",
    "fuck", "shit", "bitch"
]

UNSAFE_CHILD_TOPICS = [
    "алкоголь", "водка", "вино", "пиво", "бар", "клуб", "тусовка",
    "наркотик", "наркотики", "трава", "кокаин", "героин", "меф",
    "стрельба", "стрелять", "оружие", "пистолет", "автомат", "нож",
    "война", "бомба", "взрыв", "убийство", "убить", "кровь",
    "смерть", "ужас", "страх", "монстр", "демон", "ад",
    "секс", "эротика", "порно",
    "казино", "ставки", "азарт",
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
        ["💳 Купить колыбельные"],
        ["🌙 Создать новую колыбельную"],
        ["🏠 Главное меню"],
    ], with_nav=False)


def buy_keyboard():
    return keyboard([
        ["🌙 Купить 1 колыбельную"],
        ["🌙 Купить 2 колыбельные"],
        ["🌙 Купить 3 колыбельные"],
        [PAYMENT_CHECK_BUTTON],
        ["🏠 Главное меню"],
    ], with_nav=False)


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
    ])


# =========================
# БАЗА ДАННЫХ
# =========================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            nuts INTEGER DEFAULT 0,
            lullabies INTEGER DEFAULT 0
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

    ensure_column(cur, "users", "lullabies", "INTEGER DEFAULT 0")
    ensure_column(cur, "payments", "lullabies", "INTEGER DEFAULT 0")
    ensure_column(cur, "payments", "customer_email", "TEXT")

    conn.commit()
    conn.close()


def ensure_column(cur, table_name, column_name, column_definition):
    cur.execute(f"PRAGMA table_info({table_name})")
    existing_columns = {row[1] for row in cur.fetchall()}

    if column_name not in existing_columns:
        cur.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
        )


def create_user_if_not_exists(user):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        INSERT OR IGNORE INTO users (user_id, username, nuts)
        VALUES (?, ?, 0)
    """, (user.id, user.username))

    conn.commit()
    conn.close()


def get_nuts(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT nuts FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    conn.close()

    return row[0] if row else 0


def get_lullabies(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT lullabies FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    conn.close()

    return row[0] if row else 0


def add_nuts(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        UPDATE users
        SET nuts = nuts + ?
        WHERE user_id = ?
    """, (amount, user_id))

    conn.commit()
    conn.close()


def add_lullabies(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        UPDATE users
        SET lullabies = lullabies + ?
        WHERE user_id = ?
    """, (amount, user_id))

    conn.commit()
    conn.close()


def remove_nuts(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        UPDATE users
        SET nuts = nuts - ?
        WHERE user_id = ? AND nuts >= ?
    """, (amount, user_id, amount))

    conn.commit()
    conn.close()


def remove_lullabies(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        UPDATE users
        SET lullabies = lullabies - ?
        WHERE user_id = ? AND lullabies >= ?
    """, (amount, user_id, amount))

    changed = cur.rowcount

    conn.commit()
    conn.close()

    return changed > 0


def create_local_payment_order(user_id, package_key, customer_email):
    package = LULLABY_PACKAGES[package_key]
    local_payment_id = f"lullaby_{uuid.uuid4().hex}"

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
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
        0,
        package["lullabies"],
        package["price"],
        customer_email,
    ))

    conn.commit()
    conn.close()

    return local_payment_id


def update_payment_order(local_payment_id, yookassa_payment_id, status, confirmation_url):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        UPDATE payments
        SET yookassa_payment_id = ?,
            status = ?,
            confirmation_url = ?
        WHERE local_payment_id = ?
    """, (yookassa_payment_id, status, confirmation_url, local_payment_id))

    conn.commit()
    conn.close()


def mark_payment_status(local_payment_id, status):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        UPDATE payments
        SET status = ?
        WHERE local_payment_id = ?
    """, (status, local_payment_id))

    conn.commit()
    conn.close()


def get_latest_uncredited_payment(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT
            local_payment_id, yookassa_payment_id, package_title, lullabies,
            amount_value, currency, status, confirmation_url, credited
        FROM payments
        WHERE user_id = ? AND credited = 0
        ORDER BY created_at DESC
        LIMIT 1
    """, (user_id,))

    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "local_payment_id": row[0],
        "yookassa_payment_id": row[1],
        "package_title": row[2],
        "lullabies": row[3],
        "amount_value": row[4],
        "currency": row[5],
        "status": row[6],
        "confirmation_url": row[7],
        "credited": row[8],
    }


def credit_payment_if_needed(local_payment_id, yookassa_payment_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT user_id, lullabies, credited
        FROM payments
        WHERE local_payment_id = ? AND yookassa_payment_id = ?
    """, (local_payment_id, yookassa_payment_id))

    row = cur.fetchone()

    if not row:
        conn.close()
        return None, False

    user_id, lullabies, credited = row

    if credited:
        conn.close()
        return {"user_id": user_id, "lullabies": lullabies}, False

    cur.execute("""
        UPDATE users
        SET lullabies = lullabies + ?
        WHERE user_id = ?
    """, (lullabies, user_id))

    cur.execute("""
        UPDATE payments
        SET status = 'succeeded',
            credited = 1,
            paid_at = CURRENT_TIMESTAMP
        WHERE local_payment_id = ?
    """, (local_payment_id,))

    conn.commit()
    conn.close()

    return {"user_id": user_id, "lullabies": lullabies}, True


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
    return [word for word in UNSAFE_CHILD_TOPICS if word in low]


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

        if i == index:
            plain_chars.append(lower_char)
            stressed_chars.append(lower_char + STRESS_MARK)
        else:
            plain_chars.append(lower_char)
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


async def send_long_text(update: Update, text: str):
    max_length = 3500
    for i in range(0, len(text), max_length):
        await update.message.reply_text(text[i:i + max_length])


CREATE_TEXT_WAIT_MESSAGES = [
    (5, "🐿️ Колыбелка подбирает самые нежные слова 🌙"),
    (10, "✨ Песенка уже складывается в мягкий ритм 🎵"),
    (15, "🌙 Почти готово... проверяю, чтобы всё звучало красиво"),
    (20, "🐿️ Колыбелка чуть задумалась, сейчас всё аккуратно допоёт ✨"),
]

EDIT_TEXT_WAIT_MESSAGES = [
    (5, "✏️ Колыбелка уже вносит правки и бережёт нежный ритм 🌙"),
    (10, "✨ Ещё немного... подправляю строки, чтобы песня звучала мягче 🎵"),
    (15, "🌙 Почти готово... проверяю новые слова и окончания"),
    (20, "🐿️ Колыбелка внимательно перечитывает текст, чтобы всё было красиво ✨"),
]


async def run_with_wait_messages(update: Update, func, *args, wait_messages=None):
    task = asyncio.create_task(asyncio.to_thread(func, *args))

    if wait_messages is None:
        wait_messages = CREATE_TEXT_WAIT_MESSAGES

    elapsed = 0

    for seconds, message in wait_messages:
        delay = seconds - elapsed

        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=delay)
        except asyncio.TimeoutError:
            elapsed = seconds
            await update.message.reply_text(
                message,
                reply_markup=generation_wait_keyboard()
            )

    return await task


# =========================
# OPENAI
# =========================

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


def generate_lullaby_text(data):
    age_context = get_age_context(data)
    gender_context = get_gender_context(data)

    prompt = f"""
Ты профессиональный автор детских колыбельных.

Создай детскую колыбельную на русском языке.

Данные:
Имя ребёнка: {data["name_stressed"]}
Пол ребёнка: {data["gender"]}
Возраст ребёнка: {data["age"]}
Возрастной контекст: {age_context}
Грамматический контекст пола: {gender_context}
Персонажи и образы: {data["characters"]}
Голос для музыкальной версии: {data["voice"]}
Настроение: {data["mood"]}
Тема: {data["theme"]}

Структура:
- Куплет
- Припев
- Куплет
- Припев

Правила:
- общая длина: 500–900 символов
- припев должен повторяться одинаковым текстом
- припев должен быть коротким и легко запоминаться
- НЕ пиши слова "Куплет" или "Припев"
- текст должен идти как обычная песня
- используй только имя ребёнка: {data["name_stressed"]}
- ударение в имени уже проставлено, сохраняй его во всех повторениях имени
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

    response = OPENAI_CLIENT.responses.create(
        model="gpt-5.2",
        input=prompt
    )

    return response.output_text


def edit_lullaby_text(data, old_text, edit_request):
    age_context = get_age_context(data)
    gender_context = get_gender_context(data)

    prompt = f"""
Перепиши детскую колыбельную с учётом правки пользователя.

Данные:
Имя ребёнка: {data["name_stressed"]}
Пол ребёнка: {data["gender"]}
Возраст ребёнка: {data["age"]}
Возрастной контекст: {age_context}
Грамматический контекст пола: {gender_context}
Персонажи: {data["characters"]}
Голос для музыкальной версии: {data["voice"]}
Настроение: {data["mood"]}
Тема: {data["theme"]}

Текущий текст:
{old_text}

Правка пользователя:
{edit_request}

Правила:
- структура: Куплет → Припев → Куплет → Припев
- припев должен повторяться одинаковым текстом
- длина: 500–900 символов
- НЕ пиши слова "Куплет" или "Припев"
- используй только имя ребёнка: {data["name_stressed"]}
- сохраняй ударение в имени
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

    response = OPENAI_CLIENT.responses.create(
        model="gpt-5.2",
        input=prompt
    )

    return response.output_text


def polish_lullaby_text(data, text):
    gender_context = get_gender_context(data)

    prompt = f"""
Проверь детскую колыбельную на русском языке.

Данные:
Имя ребёнка: {data["name_stressed"]}
Пол ребёнка: {data["gender"]}
Грамматический контекст пола: {gender_context}

Задача:
- исправь грамматические ошибки
- исправь неестественные фразы
- улучши фонетику для пения
- упрости сложные слова
- структура должна быть: Куплет → Припев → Куплет → Припев
- припев должен повторяться одинаковым текстом
- сохрани мягкий ритм колыбельной
- сохрани имя ребёнка именно так: {data["name_stressed"]}
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

    response = OPENAI_CLIENT.responses.create(
        model="gpt-5.2",
        input=prompt
    )

    return response.output_text


def add_stress_marks_to_song(data, text):
    gender_context = get_gender_context(data)

    prompt = f"""
Расставь ударения в тексте русской детской песни для музыкальной генерации Suno.

Данные:
Имя ребёнка: {data["name_stressed"]}
Пол ребёнка: {data["gender"]}
Грамматический контекст пола: {gender_context}

Важно:
- добавь знак ударения ́ после ударной гласной в каждом русском слове, где это возможно
- имя ребёнка всегда пиши именно так: {data["name_stressed"]}
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

    response = OPENAI_CLIENT.responses.create(
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

    return stressed, note


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


def create_yookassa_payment(user_id, package_key, customer_email):
    package = LULLABY_PACKAGES[package_key]
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
            "lullabies": str(package["lullabies"]),
        },
        "receipt": {
            "customer": {
                "email": customer_email,
            },
            "items": [
                {
                    "description": package["title"],
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


async def send_payment_link(update: Update, context: ContextTypes.DEFAULT_TYPE, package_key, customer_email):
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
            "Попробуй выбрать пакет ещё раз.",
            reply_markup=buy_keyboard()
        )
        return

    await update.message.reply_text(
        f"🌙 Пакет: {package['title']}\n"
        f"💳 Стоимость: {package['price']} ₽\n\n"
        f"Оплати по ссылке ЮKassa:\n{payment_url}\n\n"
        f"После оплаты нажми «{PAYMENT_CHECK_BUTTON}».",
        reply_markup=buy_keyboard()
    )


async def check_latest_payment(update: Update):
    if not yookassa_is_configured():
        await update.message.reply_text(
            "😔 Оплата пока не настроена. Попробуй позже.",
            reply_markup=profile_keyboard()
        )
        return

    user_id = update.effective_user.id
    payment = get_latest_uncredited_payment(user_id)

    if not payment:
        await update.message.reply_text(
            "🌙 Я не нашла неоплаченную покупку.\n\n"
            "Выбери пакет колыбельных, и я пришлю ссылку на оплату.",
            reply_markup=buy_keyboard()
        )
        return

    if not payment["yookassa_payment_id"]:
        await update.message.reply_text(
            "😔 Платёж ещё не был создан в ЮKassa.\n\n"
            "Попробуй выбрать пакет ещё раз.",
            reply_markup=buy_keyboard()
        )
        return

    try:
        yookassa_payment = await asyncio.to_thread(
            get_yookassa_payment,
            payment["yookassa_payment_id"],
        )
    except Exception as error:
        print("Ошибка проверки платежа ЮKassa:", error)
        await update.message.reply_text(
            "😔 Не получилось проверить оплату.\n\n"
            "Попробуй ещё раз чуть позже.",
            reply_markup=buy_keyboard()
        )
        return

    status = yookassa_payment.get("status")

    if status == "succeeded" and yookassa_payment.get("paid"):
        order, credited_now = credit_payment_if_needed(
            payment["local_payment_id"],
            payment["yookassa_payment_id"],
        )

        if not order:
            await update.message.reply_text(
                "Платёж прошёл, но заказ не найден. Напиши поддержке, мы всё проверим.",
                reply_markup=main_menu_keyboard()
            )
            return

        balance = get_lullabies(order["user_id"])

        if credited_now:
            await update.message.reply_text(
                f"✅ Оплата прошла успешно!\n\n"
                f"🌙 Начислено: {order['lullabies']} колыбельных\n"
                f"Текущий баланс: {balance} колыбельных\n\n"
                f"Теперь можно создать персональную колыбельную 🌙🎵",
                reply_markup=profile_keyboard()
            )
        else:
            await update.message.reply_text(
                f"✅ Эта оплата уже была учтена.\n\n"
                f"🌙 Текущий баланс: {balance} колыбельных",
                reply_markup=profile_keyboard()
            )

        return

    if status == "canceled":
        mark_payment_status(payment["local_payment_id"], "canceled")
        await update.message.reply_text(
            "😔 Платёж отменён или истёк.\n\n"
            "Можно выбрать пакет заново.",
            reply_markup=buy_keyboard()
        )
        return

    mark_payment_status(payment["local_payment_id"], status or payment["status"])

    await update.message.reply_text(
        f"⏳ Оплата пока не прошла.\n\n"
        f"Если ты уже оплатил, подожди немного и нажми «{PAYMENT_CHECK_BUTTON}» ещё раз.\n\n"
        f"Ссылка на оплату:\n{payment['confirmation_url']}",
        reply_markup=buy_keyboard()
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
    f"💳 Сначала выбери пакет колыбельных, потом я создам текст и музыку.\n\n"
    f"💛 Давай создадим первую прямо сейчас:",
    reply_markup=main_menu_keyboard()
    )

    return START


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    return await show_main_menu(update, context)


async def offer_buy_lullabies(update: Update, user_id):
    lullabies = get_lullabies(user_id)

    await update.message.reply_text(
        f"🌙 На балансе пока нет оплаченных колыбельных.\n\n"
        f"Сейчас доступно: {lullabies}\n"
        f"Для создания текста и музыки нужна 1 оплаченная колыбельная.\n\n"
        f"Выбери пакет, и после оплаты можно будет сразу начать создание.",
        reply_markup=buy_keyboard()
    )


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
        lullabies = get_lullabies(user_id)

        await update.message.reply_text(
            f"👤 Личный кабинет\n\n"
            f"🌙 Оплаченных колыбельных: {lullabies}\n\n"
            f"Одна колыбельная включает создание текста, правки текста и музыкальную версию.\n\n"
            f"Здесь можно купить пакет или сразу создать новую колыбельную 🌙",
            reply_markup=profile_keyboard()
        )
        return START

    if text == "💳 Купить колыбельные":
        await update.message.reply_text(
            "💳 Выбери пакет колыбельных\n\n"
            "1 колыбельная — 350 ₽\n"
            "2 колыбельные — 500 ₽\n"
            "3 колыбельные — 600 ₽\n\n"
            "Оплата нужна до создания текста, потому что текст и правки тоже создаются через AI.",
            reply_markup=buy_keyboard()
        )
        return START

    if text in LULLABY_PACKAGES:
        context.user_data["pending_payment_package_key"] = text

        await update.message.reply_text(
            "📧 Напиши email для чека.\n\n"
            "ЮKassa отправит чек на этот email после оплаты.",
            reply_markup=keyboard([])
        )
        return PAYMENT_EMAIL_INPUT

    if text == PAYMENT_CHECK_BUTTON:
        await check_latest_payment(update)
        return START

    if is_create_lullaby(text):
        lullabies = get_lullabies(user_id)

        if lullabies < LULLABIES_PER_GENERATION:
            await offer_buy_lullabies(update, user_id)
            return START

        context.user_data.clear()

        await update.message.reply_text(
            "🌙 Давай создадим персональную колыбельную\n\n"
            "Сначала напиши имя ребёнка.\n\n"
            "✨ Важно: чтобы имя красиво звучало в песне, выдели ударную гласную БОЛЬШОЙ буквой.\n\n"
            "Примеры:\n"
            "МилАна\n"
            "КИра\n"
            "СофИя\n"
            "МирОн\n"
            "Ева\n"
            "АлИса",
            reply_markup=keyboard([])
        )

        return NAME_INPUT

    await update.message.reply_text(
        "🌙 Пожалуйста, выбери действие кнопкой ниже.",
        reply_markup=main_menu_keyboard()
    )
    return START


async def payment_email_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if is_restart(text):
        return await start(update, context)

    if is_back(text) or is_home(text):
        context.user_data.pop("pending_payment_package_key", None)
        await update.message.reply_text(
            "💳 Выбери пакет колыбельных:",
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

    if package_key not in LULLABY_PACKAGES:
        await update.message.reply_text(
            "😔 Не нашла выбранный пакет.\n\n"
            "Выбери пакет колыбельных ещё раз.",
            reply_markup=buy_keyboard()
        )
        return START

    context.user_data.pop("pending_payment_package_key", None)
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
        f"🎵 С ударением для песни: {stressed_name}\n\n"
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
            "Ударную гласную выдели БОЛЬШОЙ буквой.\n\n"
            "Примеры:\n"
            "МилАна\n"
            "АртЁм\n"
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
            "Напиши имя ребёнка и выдели ударную гласную БОЛЬШОЙ буквой.\n\n"
            "Например: МилАна",
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
        "мама, папа, мишка, зайка, Синий трактор\n\n"
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
🎵 Имя с ударением: {data["name_stressed"]}
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

    if not context.user_data.get("lullaby_credit_used"):
        if not remove_lullabies(user_id, LULLABIES_PER_GENERATION):
            await offer_buy_lullabies(update, user_id)
            return START

        context.user_data["lullaby_credit_used"] = True

    await update.message.reply_text(
        f"✨ {BRAND_NAME} сочиняет колыбельную и расставляет правильные ударения 🌙",
        reply_markup=generation_wait_keyboard()
    )

    try:
        lullaby_text, safety_note = await run_with_wait_messages(
            update,
            generate_and_prepare_lullaby,
            context.user_data,
            wait_messages=CREATE_TEXT_WAIT_MESSAGES
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

    except Exception as error:
        print("Ошибка OpenAI:", error)

        if context.user_data.get("lullaby_credit_used"):
            add_lullabies(update.effective_user.id, LULLABIES_PER_GENERATION)
            context.user_data.pop("lullaby_credit_used", None)

        await update.message.reply_text(
            "😔 Не получилось создать текст колыбельной.\n\n"
            "Иногда сервис отвечает слишком долго. Колыбельная вернулась на баланс, можно попробовать ещё раз.",
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
        return await generate_music(update, context)

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
            wait_messages=EDIT_TEXT_WAIT_MESSAGES
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

    except Exception as error:
        print("Ошибка OpenAI:", error)
        await update.message.reply_text(
            "😔 Не получилось изменить текст.\n\n"
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

    if not data.get("lullaby_credit_used"):
        await update.message.reply_text(
            "🌙 Сначала нужно оплатить колыбельную и создать текст.",
            reply_markup=main_menu_keyboard()
        )
        return START

    lullaby_text = data["lullaby_text"]

    await update.message.reply_text(
        f"🎵 {BRAND_NAME} создаёт музыкальную колыбельную...\n\n"
        f"Сейчас текст превратится в нежную песню для сна 🌙",
        reply_markup=generation_wait_keyboard()
    )

    try:
        genitive_name = make_genitive_name(data["name"])
        title = f"Колыбельная для {genitive_name}"
        style = make_music_style(data)

        task_id = await asyncio.to_thread(create_music_task, lullaby_text, style, title)

        audio_urls = []

        for attempt in range(30):
            await asyncio.sleep(10)
            audio_urls = await asyncio.to_thread(get_music_audio_urls, task_id)

            if audio_urls:
                break

            if attempt == 6:
                await update.message.reply_text(
                    "⏳ Музыка ещё создаётся... Уже скоро будет волшебство 🌙",
                    reply_markup=generation_wait_keyboard()
                )

            if attempt == 14:
                await update.message.reply_text(
                    "🎵 Почти готово. Собираю музыкальную колыбельную...",
                    reply_markup=generation_wait_keyboard()
                )

        if not audio_urls:
            add_lullabies(user_id, LULLABIES_PER_GENERATION)
            context.user_data.clear()

            await update.message.reply_text(
                "😔 Музыкальная колыбельная пока не готова.\n\n"
                "🌙 Колыбельная вернулась на баланс.\n\n"
                "Можно попробовать создать музыку позже или зайти в личный кабинет.",
                reply_markup=main_menu_keyboard()
            )
            return START

        safe_title = clean_filename(title)
        audio_url = audio_urls[0]

        await update.message.reply_text(
            "✨ Готово! Колыбельная создана 🎵",
            reply_markup=generation_wait_keyboard()
        )

        filename = f"{safe_title}.mp3"
        file_path = await asyncio.to_thread(download_audio, audio_url, filename)

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

        balance = get_lullabies(user_id)

        context.user_data.clear()

        await update.message.reply_text(
            f"🌙 Всё готово!\n\n"
            f"Спасибо, что создали колыбельную в {BRAND_NAME} ✨\n\n"
            f"🌙 Оплаченных колыбельных осталось: {balance}\n\n"
            f"Можно создать новую колыбельную или зайти в личный кабинет.",
            reply_markup=main_menu_keyboard()
        )

        return START

    except Exception as error:
        print("Ошибка Suno/музыки:", error)

        add_lullabies(user_id, LULLABIES_PER_GENERATION)
        context.user_data.clear()

        await update.message.reply_text(
            "😔 Не получилось создать музыкальную колыбельную.\n\n"
            "🌙 Колыбельная вернулась на баланс.\n\n"
            "Можно попробовать позже или зайти в личный кабинет.",
            reply_markup=main_menu_keyboard()
        )
        return START


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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("⚠️ Ошибка Telegram/сети:", context.error)


def main():
    init_db()

    if not TELEGRAM_TOKEN:
        print("Ошибка: TELEGRAM_TOKEN не найден")
        return

    if not OPENAI_API_KEY:
        print("Ошибка: OPENAI_API_KEY не найден")
        return

    if not SUNO_API_KEY:
        print("Ошибка: SUNO_API_KEY не найден")
        return

    if not YOOKASSA_SHOP_ID:
        print("Ошибка: YOOKASSA_SHOP_ID не найден")
        return

    if not YOOKASSA_SECRET_KEY:
        print("Ошибка: YOOKASSA_SECRET_KEY не найден")
        return

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(60)
        .read_timeout(180)
        .write_timeout(180)
        .pool_timeout(180)
        .build()
    )

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
            NAME_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_input)],
            NAME_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_confirm)],
            GENDER_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, gender_input)],
            AGE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, age_input)],
            AGE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, age_confirm)],
            CHAR_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, char_input)],
            CHAR_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, char_confirm)],
            VOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, voice_choice)],
            MOOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, mood_choice)],
            THEME: [MessageHandler(filters.TEXT & ~filters.COMMAND, theme_choice)],
            THEME_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, theme_custom_input)],
            FINAL_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, final_confirm)],
            LULLABY_REVIEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, lullaby_review)],
            EDIT_REQUEST: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_request)],
            GENERATE_MUSIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, generate_music_button)],
            PAYMENT_EMAIL_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_email_input)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^🔄 Начать заново$"), start),
            MessageHandler(filters.Regex("^🔄 Начать сначала$"), start),
            MessageHandler(filters.Regex("^Начать заново$"), start),
            MessageHandler(filters.Regex("^Начать сначала$"), start),
        ],
        allow_reentry=True,
    )

    app.add_handler(conversation)
    app.add_error_handler(error_handler)

    print("Колыбелка запущена...")
    app.run_polling()


if __name__ == "__main__":
    main()
