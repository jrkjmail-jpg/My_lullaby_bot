import os
import re
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

DB_PATH = "kolybelka.db"

# 🔥 ТВОЯ ПРАВКА
MAX_EDITS = 3
NUTS_PER_GENERATION = 2

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
    "алкоголь", "водка", "вино", "пиво", "бар", "клуб",
    "наркотик", "наркотики", "кокаин",
    "стрельба", "оружие",
    "война", "убийство", "кровь",
    "смерть", "ужас", "демон",
    "секс", "порно",
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

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            nuts INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()

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
# =========================
# ВАЛИДАЦИЯ
# =========================

def is_restart(text):
    return text in [
        "🔄 Начать заново",
        "Начать заново",
    ]

def is_back(text):
    return text in [
        "⬅️ Назад",
        "Назад",
    ]

def is_yes(text):
    return text in ["✅ Всё верно", "Да"]

def is_change(text):
    return text in ["✏️ Изменить", "Изменить"]

def is_home(text):
    return text in ["🏠 Главное меню"]

def is_create_lullaby(text):
    return text in [
        "🌙 Создать новую колыбельную",
        "Создать колыбельную",
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
        return False, "🌙 Эта тема не подходит для детской колыбельной."

    return True, ""

def validate_common(text):
    if not text or not text.strip():
        return False, "🌙 Поле не может быть пустым."

    if len(text) > 300:
        return False, "✨ Слишком длинный текст."

    if has_bad_words(text):
        return False, "🌙 Без грубых слов."

    return True, ""

def parse_age(age_text):
    age_text = age_text.strip().replace(".", ",")

    if not re.fullmatch(r"\d{1,2}(,\d{1,2})?", age_text):
        return None

    parts = age_text.split(",")
    years = int(parts[0])
    months = int(parts[1]) if len(parts) == 2 else 0

    if years > 12:
        return None

    return {
        "display": f"{years} лет" if months == 0 else f"{years} лет {months} месяцев",
        "years": years,
        "months": months
    }

def validate_age(age):
    ok, message = validate_common(age)
    if not ok:
        return False, message

    if not parse_age(age):
        return False, "🌙 Неверный формат возраста."

    return True, ""

def clean_filename(text):
    text = re.sub(r'[\\/*?:"<>|]', "", text)
    return text.strip()[:80]


# =========================
# ОТПРАВКА ДЛИННОГО ТЕКСТА
# =========================

async def send_long_text(update: Update, text: str):
    max_length = 3500
    for i in range(0, len(text), max_length):
        await update.message.reply_text(text[i:i + max_length])

# =========================
# OPENAI / ГЕНЕРАЦИЯ ТЕКСТА
# =========================

def generate_lullaby_text(data):
    prompt = f"""
Ты профессиональный автор детских колыбельных.

Создай мягкую, добрую колыбельную.

Имя: {data["name_stressed"]}
Пол: {data["gender"]}
Возраст: {data["age"]}
Тема: {data["theme"]}
Персонажи: {data["characters"]}
Настроение: {data["mood"]}

Структура:
Куплет → Припев → Куплет → Припев

Правила:
- мягкий ритм
- короткие строки
- легко поётся
- без страшных образов
- без взрослых тем
- без плохих слов
- припев повторяется одинаково
- текст 500–900 символов
- не пиши "куплет" и "припев"

Верни только текст песни.
"""

    response = OPENAI_CLIENT.responses.create(
        model="gpt-5.2",  # 🔥 НЕ МЕНЯЕМ
        input=prompt
    )

    return response.output_text


def edit_lullaby_text(data, old_text, edit_request):
    prompt = f"""
Перепиши колыбельную с учётом правки.

Текущий текст:
{old_text}

Правка:
{edit_request}

Сохрани:
- структуру
- мягкость
- ритм

Верни только новый текст песни.
"""

    response = OPENAI_CLIENT.responses.create(
        model="gpt-5.2",
        input=prompt
    )

    return response.output_text


def generate_and_prepare_lullaby(data):
    return generate_lullaby_text(data)


def edit_and_prepare_lullaby(data, old_text, edit_request):
    return edit_lullaby_text(data, old_text, edit_request)
# =========================
# ОЖИДАНИЕ С СООБЩЕНИЯМИ
# =========================

CREATE_TEXT_WAIT_MESSAGES = [
    (10, "🐿️ Колыбелка подбирает самые нежные слова 🌙"),
    (20, "✨ Песенка уже складывается в мягкий ритм 🎵"),
    (30, "🌙 Почти готово... проверяю звучание"),
    (40, "🐿️ Колыбелка допевает последние строчки ✨"),
]

EDIT_TEXT_WAIT_MESSAGES = [
    (10, "✏️ Колыбелка вносит правки 🌙"),
    (20, "✨ Улучшаю ритм и мягкость 🎵"),
    (30, "🌙 Почти готово..."),
    (40, "🐿️ Проверяю текст ещё раз ✨"),
]


async def run_with_wait_messages(update: Update, func, *args, wait_messages=None):
    task = asyncio.create_task(asyncio.to_thread(func, *args))

    if wait_messages is None:
        wait_messages = CREATE_TEXT_WAIT_MESSAGES

    elapsed = 0

    for seconds, message in wait_messages:
        delay = seconds - elapsed

        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=delay
            )
        except asyncio.TimeoutError:
            elapsed = seconds

            await update.message.reply_text(
                message,
                reply_markup=generation_wait_keyboard()
            )

    return await task
# =========================
# SUNO / ГЕНЕРАЦИЯ МУЗЫКИ
# =========================

def make_music_style(data):
    voice = data["voice"]

    if voice == "👩 Женский голос":
        return "soft female lullaby, calm, warm, piano"
    elif voice == "👨 Мужской голос":
        return "soft male lullaby, calm, deep, gentle"
    else:
        return "child voice lullaby, soft, innocent"


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
        "title": title
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
    headers = {"Authorization": f"Bearer {SUNO_API_KEY}"}

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
        if song.get("audioUrl"):
            urls.append(song["audioUrl"])

    return urls


def download_audio(url, filename):
    response = requests.get(url, timeout=120)
    response.raise_for_status()

    path = os.path.join(tempfile.gettempdir(), filename)

    with open(path, "wb") as f:
        f.write(response.content)

    return path


# =========================
# ГЕНЕРАЦИЯ МУЗЫКИ (🔥 ТВОЙ UX)
# =========================

async def generate_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    nuts = get_nuts(user_id)

    if nuts < NUTS_PER_GENERATION:
        await update.message.reply_text("🌰 Не хватает орешков")
        return START

    remove_nuts(user_id, NUTS_PER_GENERATION)

    data = context.user_data
    text = data["lullaby_text"]

    await update.message.reply_text(
        "🎵 Колыбелка напевает колыбельную...\n\n"
        "Создаю музыку для сна 🌙"
    )

    try:
        task_id = await asyncio.to_thread(
            create_music_task,
            text,
            make_music_style(data),
            "Колыбельная"
        )

        total_wait = 0
        max_wait = 180  # 3 минуты
        audio_urls = []

        while total_wait < max_wait:
            await asyncio.sleep(10)
            total_wait += 10

            audio_urls = await asyncio.to_thread(
                get_music_audio_urls,
                task_id
            )

            if audio_urls:
                break

            # 🔥 ПРОГРЕСС
            if total_wait == 60:
                await update.message.reply_text("⏳ Осталось примерно 2 минуты...")
            elif total_wait == 120:
                await update.message.reply_text("⏳ Осталась примерно 1 минута...")
            else:
                await update.message.reply_text("🎵 Колыбелка поёт...")

        if not audio_urls:
            add_nuts(user_id, NUTS_PER_GENERATION)

            await update.message.reply_text(
                "😔 Не удалось создать музыку.\n"
                "🌰 Орешки возвращены."
            )
            return START

        file_path = await asyncio.to_thread(
            download_audio,
            audio_urls[0],
            "lullaby.mp3"
        )

        with open(file_path, "rb") as f:
            await update.message.reply_document(f)

        os.remove(file_path)

        await update.message.reply_text("✨ Колыбельная готова!")

        return START

    except Exception as e:
        print("Ошибка:", e)

        add_nuts(user_id, NUTS_PER_GENERATION)

        await update.message.reply_text(
            "😔 Ошибка при создании музыки.\n"
            "🌰 Орешки возвращены."
        )

        return START
# =========================
# МЕНЮ / СТАРТ
# =========================

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    create_user_if_not_exists(update.effective_user)

    await update.message.reply_text(
        f"🌙 Добро пожаловать в {BRAND_NAME}!\n\n"
        f"Создаю персональные колыбельные 🎵\n\n"
        f"1 колыбельная = {NUTS_PER_GENERATION} орешка 🌰🌰",
        reply_markup=main_menu_keyboard()
    )

    return START


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    return await show_main_menu(update, context)


async def offer_buy_nuts(update: Update, user_id):
    nuts = get_nuts(user_id)

    await update.message.reply_text(
        f"🌰 Баланс: {nuts}\n\n"
        f"Нужно: {NUTS_PER_GENERATION}",
        reply_markup=buy_keyboard()
    )


# =========================
# ОБРАБОТКА ГЛАВНОГО МЕНЮ
# =========================

async def start_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    create_user_if_not_exists(update.effective_user)

    if is_restart(text):
        return await start(update, context)

    if is_home(text):
        return await show_main_menu(update, context)

    if text == "👤 Личный кабинет":
        nuts = get_nuts(user_id)

        await update.message.reply_text(
            f"👤 Баланс: {nuts} 🌰",
            reply_markup=profile_keyboard()
        )
        return START

    if text == "🌰 Купить орешки":
        await update.message.reply_text(
            "Выбери пакет",
            reply_markup=buy_keyboard()
        )
        return START

    if text in NUT_PACKAGES:
        add_nuts(user_id, NUT_PACKAGES[text])

        await update.message.reply_text(
            "Орешки начислены 🌰",
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
            "Напиши имя ребёнка"
        )

        return NAME_INPUT

    return START
# =========================
# ИМЯ
# =========================

async def name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()

    ok, message = validate_common(name)
    if not ok:
        await update.message.reply_text(message)
        return NAME_INPUT

    context.user_data["name"] = name
    context.user_data["name_stressed"] = name

    await update.message.reply_text(
        f"Имя: {name}\n\nВсё верно?",
        reply_markup=yes_change_keyboard()
    )

    return NAME_CONFIRM


async def name_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_change(text):
        await update.message.reply_text("Напиши имя заново")
        return NAME_INPUT

    context.user_data["gender"] = None

    await update.message.reply_text(
        "Выбери пол",
        reply_markup=gender_keyboard()
    )

    return GENDER_INPUT


# =========================
# ПОЛ
# =========================

async def gender_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text not in ["👧 Девочка", "👦 Мальчик"]:
        await update.message.reply_text("Выбери кнопкой")
        return GENDER_INPUT

    context.user_data["gender"] = text

    await update.message.reply_text("Напиши возраст")

    return AGE_INPUT


# =========================
# ВОЗРАСТ
# =========================

async def age_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    age = update.message.text

    parsed = parse_age(age)

    if not parsed:
        await update.message.reply_text(
            "🌙 Возраст нужно написать так:\n\n2\n3,5\n3,10"
        )
        return AGE_INPUT

    context.user_data["age"] = parsed["display"]

    await update.message.reply_text(
        f"Возраст: {parsed['display']}\n\nВсё верно?",
        reply_markup=yes_change_keyboard()
    )

    return AGE_CONFIRM


async def age_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_change(text):
        await update.message.reply_text("Напиши возраст заново")
        return AGE_INPUT

    await update.message.reply_text(
        "Кого добавить в песню?",
    )

    return CHAR_INPUT


# =========================
# ПЕРСОНАЖИ
# =========================

async def char_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    ok, message = validate_common(text)
    if not ok:
        await update.message.reply_text(message)
        return CHAR_INPUT

    context.user_data["characters"] = text

    await update.message.reply_text(
        f"Персонажи: {text}\n\nВсё верно?",
        reply_markup=yes_change_keyboard()
    )

    return CHAR_CONFIRM


async def char_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_change(text):
        await update.message.reply_text("Напиши заново")
        return CHAR_INPUT

    await update.message.reply_text(
        "Выбери голос",
        reply_markup=keyboard([
            ["👩 Женский голос"],
            ["👨 Мужской голос"],
            ["🧒 Детский голос"]
        ])
    )

    return VOICE
# =========================
# НАСТРОЕНИЕ
# =========================

async def voice_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["voice"] = update.message.text

    await update.message.reply_text(
        "Выбери настроение",
        reply_markup=keyboard([
            ["💗 Очень нежная"],
            ["🌟 Волшебная"],
            ["🌙 Спокойная"],
        ])
    )

    return MOOD


async def mood_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mood"] = update.message.text

    await update.message.reply_text(
        "Выбери тему",
        reply_markup=keyboard([
            ["🌙 Звёзды"],
            ["🌲 Лес"],
            ["☁️ Облака"],
        ])
    )

    return THEME


# =========================
# ТЕМА
# =========================

async def theme_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["theme"] = update.message.text

    await update.message.reply_text(
        "✨ Создаю текст...",
        reply_markup=generation_wait_keyboard()
    )

    lullaby_text = await run_with_wait_messages(
        update,
        generate_and_prepare_lullaby,
        context.user_data
    )

    context.user_data["lullaby_text"] = lullaby_text
    context.user_data["edit_count"] = 0

    await send_long_text(update, lullaby_text)

    await update.message.reply_text(
        "Текст подходит?",
        reply_markup=text_review_keyboard()
    )

    return LULLABY_REVIEW


# =========================
# РЕДАКТИРОВАНИЕ
# =========================

async def lullaby_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "✅ Подтвердить":
        return await generate_music(update, context)

    if text == "✏️ Редактировать":
        edits = context.user_data.get("edit_count", 0)

        if edits >= MAX_EDITS:
            await update.message.reply_text(
                "Лимит правок достигнут",
                reply_markup=create_music_keyboard()
            )
            return GENERATE_MUSIC

        await update.message.reply_text("Напиши правку")
        return EDIT_REQUEST

    return LULLABY_REVIEW


async def edit_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    edit_text = update.message.text

    new_text = await run_with_wait_messages(
        update,
        edit_and_prepare_lullaby,
        context.user_data,
        context.user_data["lullaby_text"],
        edit_text,
        wait_messages=EDIT_TEXT_WAIT_MESSAGES
    )

    context.user_data["lullaby_text"] = new_text
    context.user_data["edit_count"] += 1

    await send_long_text(update, new_text)

    await update.message.reply_text(
        "Теперь подходит?",
        reply_markup=text_review_keyboard()
    )

    return LULLABY_REVIEW
# =========================
# ЗАПУСК
# =========================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    await update.message.reply_text(
        "Отменено",
        reply_markup=main_menu_keyboard()
    )

    return START


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("Ошибка:", context.error)


def main():
    init_db()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
        ],
        states={
            START: [MessageHandler(filters.TEXT, start_button)],

            NAME_INPUT: [MessageHandler(filters.TEXT, name_input)],
            NAME_CONFIRM: [MessageHandler(filters.TEXT, name_confirm)],

            GENDER_INPUT: [MessageHandler(filters.TEXT, gender_input)],

            AGE_INPUT: [MessageHandler(filters.TEXT, age_input)],
            AGE_CONFIRM: [MessageHandler(filters.TEXT, age_confirm)],

            CHAR_INPUT: [MessageHandler(filters.TEXT, char_input)],
            CHAR_CONFIRM: [MessageHandler(filters.TEXT, char_confirm)],

            VOICE: [MessageHandler(filters.TEXT, voice_choice)],
            MOOD: [MessageHandler(filters.TEXT, mood_choice)],

            THEME: [MessageHandler(filters.TEXT, theme_choice)],

            LULLABY_REVIEW: [MessageHandler(filters.TEXT, lullaby_review)],
            EDIT_REQUEST: [MessageHandler(filters.TEXT, edit_request)],

            GENERATE_MUSIC: [MessageHandler(filters.TEXT, generate_music)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
        ],
    )

    app.add_handler(conversation)
    app.add_error_handler(error_handler)

    print("Колыбелка запущена...")
    app.run_polling()


if __name__ == "__main__":
    main()
