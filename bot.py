import logging
import random
import json
import os
import asyncio
import time
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

import gspread
from groq import Groq
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])


# ─── Веб-сервер ──────────────────────────────────────────────────────────────

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def log_message(self, format, *args):
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()


# ─── Google Sheets ───────────────────────────────────────────────────────────

def get_random_assignment():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    gc = gspread.authorize(creds)

    spreadsheet = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    sheet_name = os.environ.get("SHEET_NAME", "Лист3")
    ws = spreadsheet.worksheet(sheet_name)

    all_rows = ws.get_all_values()
    if len(all_rows) < 2:
        raise ValueError("Таблица пустая или только заголовок")

    header = all_rows[0]

    hint_col_idx = None
    for i, h in enumerate(header):
        if "обратить" in h.lower() or "внимание" in h.lower():
            hint_col_idx = i
            break

    last_student_col = hint_col_idx - 1 if hint_col_idx else len(header) - 1
    student_names = header[1:last_student_col + 1]
    students_with_idx = [(name, idx + 1) for idx, name in enumerate(student_names) if name.strip()]

    data_rows = [row for row in all_rows[1:] if row and row[0].strip()]

    if not data_rows or not students_with_idx:
        raise ValueError("Нет данных в таблице")

    row = random.choice(data_rows)
    student_name, col_idx = random.choice(students_with_idx)

    topic = row[0].strip()
    assignment = row[col_idx].strip() if col_idx < len(row) else ""
    hint = row[hint_col_idx].strip() if hint_col_idx and hint_col_idx < len(row) else ""

    return student_name, topic, assignment, hint


# ─── Генерация вопроса ───────────────────────────────────────────────────────

def generate_question(student: str, topic: str, assignment: str, hint: str) -> dict:
    hint_block = f"\nДополнительный акцент: {hint}" if hint else ""

    # Многошаговый промпт: факты → вопрос → проверка → финальный JSON
    prompt = f"""Ты составляешь вопрос для викторины. Работай строго по шагам.

Ученик: {student}
Тема: {topic}
Что должен знать ученик: {assignment}{hint_block}

ШАГ 1 — ФАКТЫ: напиши 3-5 фактов которые ты знаешь достоверно про "{assignment}". Если сомневаешься в факте — не пиши его совсем.

ШАГ 2 — ЧЕРНОВИК: составь вопрос и 4 варианта ответа на основе фактов из шага 1.

ШАГ 3 — ПРОВЕРКА: перечитай черновик и ответь на каждый из этих вопросов:
- Правильный ответ точно верен? (да/нет)
- Все 4 варианта одного типа — все имена, или все числа, или все города? (да/нет)
- Ни один неправильный вариант случайно не является правильным? (да/нет)
- Если хоть один ответ "нет" — исправь черновик.

ШАГ 4 — JSON: напиши финальный проверенный результат строго в этом формате:

JSON:
{{
  "question": "вопрос, начинающийся с обращения по имени {student}",
  "options": ["вариант 1", "вариант 2", "вариант 3", "вариант 4"],
  "correct_index": 0
}}

Дополнительные правила:
- correct_index перемешивай каждый раз — не всегда 0
- Язык всего ответа: русский"""

    for attempt in range(4):
        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=900,
                temperature=0.5,
            )
            raw = response.choices[0].message.content.strip()
            if "JSON:" in raw:
                raw = raw.split("JSON:")[-1].strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            return json.loads(raw)
        except Exception as e:
            if attempt < 3:
                wait = 5 * (2 ** attempt)  # 5, 10, 20 сек
                logger.warning(f"Groq ошибка (попытка {attempt+1}): {e}. Жду {wait} сек...")
                time.sleep(wait)
            else:
                raise



# ─── Отправка вопроса ────────────────────────────────────────────────────────

async def send_daily_question(bot, chat_id=None):
    if chat_id is None:
        chat_id = os.environ["TELEGRAM_CHAT_ID"]
    try:
        student, topic, assignment, hint = get_random_assignment()
        logger.info(f"Генерирую вопрос: ученик={student}, тема={topic}")

        quiz = generate_question(student, topic, assignment, hint)
        options = quiz["options"]

        # Если все варианты короткие — кнопки с полным текстом (как раньше)
        # Если хоть один не влезает в лимит Telegram (64 символа) — кнопки-буквы
        fits_on_buttons = all(len(opt) <= 64 for opt in options)

        if fits_on_buttons:
            keyboard = []
            for i, option in enumerate(options):
                callback = f"answer|{quiz['correct_index']}|{i}"
                keyboard.append([InlineKeyboardButton(option, callback_data=callback)])

            await bot.send_message(
                chat_id=chat_id,
                text=f"🎓 *Тема* — {topic}\n\n{quiz['question']}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            letters = ["А", "Б", "В", "Г"]
            options_text = "\n".join(
                f"{letters[i]}) {opt}" for i, opt in enumerate(options)
            )
            keyboard = []
            for i, option in enumerate(options):
                callback = f"answer|{quiz['correct_index']}|{i}"
                keyboard.append(InlineKeyboardButton(letters[i], callback_data=callback))

            await bot.send_message(
                chat_id=chat_id,
                text=f"🎓 *Тема* — {topic}\n\n{quiz['question']}\n\n{options_text}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([keyboard]),
            )

        logger.info("Вопрос отправлен!")

    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        await bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ Не удалось сгенерировать вопрос: {e}",
        )


# ─── Обработка кнопок ────────────────────────────────────────────────────────

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, correct_index, chosen_index = query.data.split("|")
    correct_index = int(correct_index)
    chosen_index = int(chosen_index)

    letters = ["А", "Б", "В", "Г"]
    keyboard_buttons = query.message.reply_markup.inline_keyboard

    # Определяем формат: если кнопки — это сами буквы А/Б/В/Г, варианты лежат в тексте сообщения
    is_letter_format = all(
        row[0].text in letters for row in keyboard_buttons
    )

    if is_letter_format:
        message_lines = query.message.text.split("\n")
        options = {}
        for line in message_lines:
            for letter in letters:
                prefix = f"{letter}) "
                if line.startswith(prefix):
                    options[letter] = line[len(prefix):].strip()
        correct_text = options.get(letters[correct_index], "?")
        chosen_text = options.get(letters[chosen_index], "?")
    else:
        options = [row[0].text for row in keyboard_buttons]
        correct_text = options[correct_index]
        chosen_text = options[chosen_index]

    user_name = query.from_user.first_name or "Участник"

    if chosen_index == correct_index:
        result_text = f"✅ *{user_name}*, правильно!\n\n*{correct_text}* — верный ответ 🎉"
    else:
        result_text = (
            f"❌ *{user_name}*, неверно.\n\n"
            f"Ты выбрал: _{chosen_text}_\n"
            f"Правильный ответ: *{correct_text}*"
        )

    more_button = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Ещё вопрос", callback_data="more_question")]
    ])

    await query.edit_message_text(
        text=query.message.text + f"\n\n{result_text}",
        parse_mode="Markdown",
        reply_markup=more_button,
    )


async def handle_more_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    chat_id = str(query.message.chat_id)
    await send_daily_question(context.bot, chat_id=chat_id)


# ─── Главная async функция ───────────────────────────────────────────────────

async def main_async():
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = Application.builder().token(token).build()
    app.add_handler(CallbackQueryHandler(handle_answer, pattern=r"^answer\|"))
    app.add_handler(CallbackQueryHandler(handle_more_question, pattern=r"^more_question$"))

    scheduler = AsyncIOScheduler()

    schedule = [
        (int(os.environ.get("SEND_HOUR_1", "9")),  int(os.environ.get("SEND_MINUTE_1", "0"))),
    ]

    for hour, minute in schedule:
        scheduler.add_job(
            send_daily_question,
            trigger="cron",
            hour=hour,
            minute=minute,
            args=[app.bot],
        )

    scheduler.start()
    times = ", ".join(f"{h:02d}:{m:02d} UTC" for h, m in schedule)
    logger.info(f"Планировщик запущен. Вопрос в {times} (12:00 МСК)")

    async with app:
        await app.start()
        await app.updater.start_polling()
        logger.info("Бот запущен, жду сообщений...")
        await asyncio.Event().wait()


def main():
    Thread(target=run_web_server, daemon=True).start()
    logger.info("Веб-сервер запущен")
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
