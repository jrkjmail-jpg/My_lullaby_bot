import os
import re
import time
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

OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY)
SUNO_BASE_URL = "https://api.sunoapi.org"

DB_PATH = os.getenv("DB_PATH", "kolybelka.db")

MAX_EDITS = 5
NUTS_PER_GENERATION = 2

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")
SUNO_MODEL = os.getenv("SUNO_MODEL", "V5_5")

NUT_PACKAGES = {
    "🌰 Купить 3 орешка": 3,
    "🌰 Купить 5 орешков": 5,
    "🌰 Купить 10 орешков": 10,
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
) = range(16)

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

# Защита от двойной генерации музыки одним пользователем
USER_MUSIC_LOCKS = set()


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


def buy_keyboard():
    return keyboard([
        ["🌰 Купить 3 орешка"],
        ["🌰 Купить 5 орешков"],
        ["🌰 Купить 10 орешков"],
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

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            nuts INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            updated_at INTEGER DEFAULT (strftime('%s','now'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            child_name TEXT,
            title TEXT,
            lyrics TEXT,
            status TEXT DEFAULT 'created',
            task_id TEXT,
            audio_url TEXT,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        )
    """)

    conn.commit()
    conn.close()


def create_user_if_not_exists(user):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO users (user_id, username, nuts)
        VALUES (?, ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            updated_at = strftime('%s','now')
    """, (user.id, user.username))

    conn.commit()
    conn.close()


def get_nuts(user_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT nuts FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    conn.close()
    return row[0] if row else 0


def add_nuts(user_id, amount):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        UPDATE users
        SET nuts = nuts + ?, updated_at = strftime('%s','now')
        WHERE user_id = ?
    """, (amount, user_id))

    conn.commit()
    conn.close()


def remove_nuts_safe(user_id, amount):
    """
    Атомарно списывает орешки.
    Возвращает True, если списание реально произошло.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("""
            UPDATE users
            SET nuts = nuts - ?, updated_at = strftime('%s','now')
            WHERE user_id = ? AND nuts >= ?
        """, (amount, user_id, amount))

        success = cur.rowcount > 0
        conn.commit()
        return success

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def save_generation(user_id, child_name, title, lyrics, status="created", task_id=None, audio_url=None):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO generations (user_id, child_name, title, lyrics, status, task_id, audio_url)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, child_name, title, lyrics, status, task_id, audio_url))

    generation_id = cur.lastrowid
    conn.commit()
    conn.close()
    return generation_id


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

    _, _, stress_error = make_stressed_name(name)

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

def openai_text_response(prompt, retries=3):
    last_error = None

    for attempt in range(retries):
        try:
            response = OPENAI_CLIENT.responses.create(
                model=OPENAI_MODEL,
                input=prompt,
            )
            return response.output_text.strip()
        except Exception as error:
            last_error = error
            if attempt < retries - 1:
                time.sleep(2 + attempt * 2)

    raise last_error


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

    return openai_text_response(prompt)


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

    return openai_text_response(prompt)


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

    return openai_text_response(prompt)


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

    return openai_text_response(prompt)


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
    data_copy = dict(data)
    raw_text = generate_lullaby_text(data_copy)
    return prepare_final_lyrics(data_copy, raw_text)


def edit_and_prepare_lullaby(data, old_text, edit_request):
    data_copy = dict(data)
    raw_new_text = edit_lullaby_text(data_copy, old_text, edit_request)
    return prepare_final_lyrics(data_copy, raw_new_text)


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


def suno_request(method, url, retries=3, **kwargs):
    last_error = None

    for attempt in range(retries):
        try:
            response = requests.request(method, url, timeout=kwargs.pop("timeout", 60), **kwargs)
            response.raise_for_status()
            return response
        except Exception as error:
            last_error = error
            if attempt < retries - 1:
                time.sleep(3 + attempt * 3)

    raise last_error


def create_music_task(lyrics, style, title):
    headers = {
        "Authorization": f"Bearer {SUNO_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "customMode": True,
        "instrumental": False,
        "model": SUNO_MODEL,
        "prompt": lyrics,
        "style": style,
        "title": title,
        "callBackUrl": "https://example.com/callback"
    }

    response = suno_request(
        "POST",
        f"{SUNO_BASE_URL}/api/v1/generate",
        headers=headers,
        json=data,
        timeout=60,
    )

    result = response.json()
    return result["data"]["taskId"]


def get_music_audio_urls(task_id):
    headers = {
        "Authorization": f"Bearer {SUNO_API_KEY}"
    }

    response = suno_request(
        "GET",
        f"{SUNO_BASE_URL}/api/v1/generate/record-info",
        headers=headers,
        params={"taskId": task_id},
        timeout=60,
    )

    result = response.json()
    data = result.get("data") or {}

    if data.get("status") != "SUCCESS":
        return []

    songs = (data.get("response") or {}).get("sunoData", [])
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
        f"🌰 1 музыкальная колыбельная = {NUTS_PER_GENERATION} орешка\n\n"
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
        f"🐿️ {BRAND_NAME} сгрызла все орешки!\n\n"
        f"🌰 Сейчас на балансе: {nuts} орешков\n"
        f"🎵 Для одной музыкальной колыбельной нужно: {NUTS_PER_GENERATION} орешка\n\n"
        f"Можно докупить орешки и сразу продолжить волшебство 🌙",
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
        nuts = get_nuts(user_id)

        await update.message.reply_text(
            f"👤 Личный кабинет\n\n"
            f"🌰 Твой баланс: {nuts} орешков\n\n"
            f"🎵 1 музыкальная колыбельная = {NUTS_PER_GENERATION} орешка\n\n"
            f"Здесь можно пополнить баланс или сразу создать новую колыбельную 🌙",
            reply_markup=profile_keyboard()
        )
        return START

    if text == "🌰 Купить орешки":
        await update.message.reply_text(
            "🌰 Выбери пакет орешков\n\n"
            "Орешки нужны, чтобы создавать музыкальные колыбельные 🎵\n\n"
            "Сейчас покупка работает в тестовом режиме.",
            reply_markup=buy_keyboard()
        )
        return START

    if text in NUT_PACKAGES:
        amount = NUT_PACKAGES[text]
        add_nuts(user_id, amount)
        balance = get_nuts(user_id)

        await update.message.reply_text(
            f"✅ Готово! {BRAND_NAME} получила новые орешки 🌰\n\n"
            f"Начислено: {amount} орешков\n"
            f"Текущий баланс: {balance} орешков\n\n"
            f"Теперь можно создать музыкальную колыбельную 🌙🎵",
            reply_markup=profile_keyboard()
        )
        return START

    if is_create_lullaby(text):
        nuts = get_nuts(user_id)

        if nuts < NUTS_PER_GENERATION:
            await offer_buy_nuts(update, user_id)
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
        await update.message.reply_text(
            "😔 Не получилось создать текст колыбельной.\n\n"
            "Иногда сервис отвечает слишком долго. Можно попробовать ещё раз или начать заново.",
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

    if user_id in USER_MUSIC_LOCKS:
        await update.message.reply_text(
            "🎵 Музыкальная колыбельная уже создаётся.\n\n"
            "Пожалуйста, дождись результата 🌙",
            reply_markup=generation_wait_keyboard()
        )
        return GENERATE_MUSIC

    if "lullaby_text" not in context.user_data:
        await update.message.reply_text(
            "🌙 Текст колыбельной не найден. Давай начнём создание заново.",
            reply_markup=main_menu_keyboard()
        )
        return START

    USER_MUSIC_LOCKS.add(user_id)

    try:
        if not remove_nuts_safe(user_id, NUTS_PER_GENERATION):
            await offer_buy_nuts(update, user_id)
            return START

        data = dict(context.user_data)
        lullaby_text = data["lullaby_text"]

        await update.message.reply_text(
            f"🎵 {BRAND_NAME} создаёт музыкальную колыбельную...\n\n"
            f"Сейчас текст превратится в нежную песню для сна 🌙",
            reply_markup=generation_wait_keyboard()
        )

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
            add_nuts(user_id, NUTS_PER_GENERATION)

            await update.message.reply_text(
                "😔 Музыкальная колыбельная пока не готова.\n\n"
                "🌰 Орешки вернулись на баланс.\n\n"
                "Можно попробовать создать музыку позже или зайти в личный кабинет.",
                reply_markup=main_menu_keyboard()
            )
            return START

        audio_url = audio_urls[0]
        save_generation(
            user_id=user_id,
            child_name=data.get("name"),
            title=title,
            lyrics=lullaby_text,
            status="success",
            task_id=task_id,
            audio_url=audio_url,
        )

        safe_title = clean_filename(title)

        await update.message.reply_text(
            "✨ Готово! Колыбельная создана 🎵",
            reply_markup=generation_wait_keyboard()
        )

        filename = f"{safe_title}.mp3"
        file_path = await asyncio.to_thread(download_audio, audio_url, filename)

        try:
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
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

        balance = get_nuts(user_id)
        context.user_data.clear()

        await update.message.reply_text(
            f"🌙 Всё готово!\n\n"
            f"Спасибо, что создали колыбельную в {BRAND_NAME} ✨\n\n"
            f"🌰 Баланс: {balance} орешков\n\n"
            f"Можно создать новую колыбельную или зайти в личный кабинет.",
            reply_markup=main_menu_keyboard()
        )

        return START

    except Exception as error:
        print("Ошибка Suno/музыки:", error)
        add_nuts(user_id, NUTS_PER_GENERATION)

        await update.message.reply_text(
            "😔 Не получилось создать музыкальную колыбельную.\n\n"
            "🌰 Орешки вернулись на баланс.\n\n"
            "Можно попробовать позже или зайти в личный кабинет.",
            reply_markup=main_menu_keyboard()
        )
        return START

    finally:
        USER_MUSIC_LOCKS.discard(user_id)


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

    try:
        if update and getattr(update, "message", None):
            await update.message.reply_text(
                "🌙 Что-то пошло не так. Попробуй ещё раз или нажми «🔄 Начать заново».",
                reply_markup=main_menu_keyboard()
            )
    except Exception as error:
        print("Не удалось отправить сообщение об ошибке:", error)


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
