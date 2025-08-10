# bot.py
from pyrogram import Client, filters
from dotenv import load_dotenv
import os
import sys
import requests
import traceback
from collections import defaultdict
from io import BytesIO
import logging
import signal

# -------- ЛОГИ --------
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("bot")

# -------- ОКРУЖЕНИЕ --------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID_STR = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

# Текст (OpenRouter)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OR_TEXT_MODEL = os.getenv("OR_TEXT_MODEL", "openai/gpt-oss-120b")

# Картинки (Hugging Face)
HF_TOKEN = os.getenv("HF_TOKEN")
HF_IMAGE_MODEL = os.getenv("HF_IMAGE_MODEL", "prompthero/openjourney")  # <- по умолчанию openjourney

missing = [k for k, v in {
    "BOT_TOKEN": BOT_TOKEN,
    "API_ID": API_ID_STR,
    "API_HASH": API_HASH,
    "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
    "HF_TOKEN": HF_TOKEN,  # обязателен для /img
}.items() if not v]
if missing:
    log.error("❌ Не заданы переменные окружения: %s", ", ".join(missing))
    sys.exit(1)

try:
    API_ID = int(API_ID_STR)
except Exception:
    log.error("❌ API_ID должен быть числом, получено: %r", API_ID_STR)
    sys.exit(1)

# -------- УТИЛИТЫ --------
def or_headers(title: str = "TelegramBot"):
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "HTTP-Referer": "https://openrouter.ai",
        "X-Title": title,
    }

# -------- ПАМЯТЬ ДИАЛОГА --------
chat_history = defaultdict(list)
HISTORY_LIMIT = 10

def clamp_history(history):
    return history[-HISTORY_LIMIT:] if len(history) > HISTORY_LIMIT else history

# -------- PYROGRAM --------
app = Client("my_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# -------- КОМАНДЫ --------
@app.on_message(filters.command("start"))
def start_handler(_, message):
    uid = message.from_user.id
    chat_history[uid] = []
    message.reply_text(
        "Привет! Я бот с памятью и генерацией картинок 🤖\n"
        "— Просто пиши, я учитываю последние 10 реплик.\n"
        "— Картинка: /img кот в космосе\n"
        "— Сбросить контекст: /reset"
    )

@app.on_message(filters.command("reset"))
def reset_handler(_, message):
    uid = message.from_user.id
    chat_history[uid] = []
    message.reply_text("🧹 Память очищена!")

# -------- ТЕКСТ (OpenRouter) --------
@app.on_message(filters.text & ~filters.command(["start", "reset", "img"]))
def text_handler(_, message):
    uid = message.from_user.id
    user_text = message.text

    chat_history[uid].append({"role": "user", "content": user_text})
    chat_history[uid] = clamp_history(chat_history[uid])

    try:
        payload = {
            "model": OR_TEXT_MODEL,
            "messages": [
                {"role": "system", "content": "Ты — дружелюбный Telegram-бот. Отвечай кратко и по делу."},
                *chat_history[uid],
            ],
        }

        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=or_headers("TelegramBotWithMemory"),
            json=payload,
            timeout=60,
            allow_redirects=False
        )

        log.info("TEXT %s | %s", resp.status_code, resp.headers.get("content-type", ""))

        if resp.status_code != 200:
            snippet = (resp.text or "")[:600]
            message.reply_text(f"❌ OpenRouter {resp.status_code}\n{snippet}")
            return

        data = resp.json()
        bot_reply = data["choices"][0]["message"]["content"].strip()

        chat_history[uid].append({"role": "assistant", "content": bot_reply})
        chat_history[uid] = clamp_history(chat_history[uid])

        message.reply_text(bot_reply or "🤖 (пустой ответ)")

    except Exception:
        traceback.print_exc()
        message.reply_text("Произошла ошибка при общении с OpenRouter 🤖")

# -------- КАРТИНКИ (Hugging Face: prompthero/openjourney) --------
@app.on_message(filters.command("img"))
def image_handler(_, message):
    prompt = " ".join(message.command[1:]).strip()
    if not prompt:
        message.reply_text("Напиши описание после команды, пример:\n/img кот в космосе, неон, 4k")
        return

    try:
        # Для открытой отличной модели openjourney:
        # https://huggingface.co/prompthero/openjourney
        url = f"https://api-inference.huggingface.co/models/{HF_IMAGE_MODEL}"
        headers = {
            "Authorization": f"Bearer {HF_TOKEN}",
            "Accept": "image/png"
        }
        payload = {
            "inputs": prompt,
            "options": {
                "wait_for_model": True  # дождаться прогрева
            }
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=180)
        ct = resp.headers.get("content-type", "")
        log.info("IMG %s | %s | model=%s", resp.status_code, ct, HF_IMAGE_MODEL)

        # Успех — отдали картинку
        if resp.status_code == 200 and ct.startswith("image/"):
            bio = BytesIO(resp.content)
            bio.name = "image.png"
            message.reply_photo(bio, caption=f"🎨 По запросу: {prompt}")
            return

        # Если пришёл JSON/текст — покажем фрагмент для диагностики
        snippet = (resp.text or "")[:800]
        message.reply_text(f"❌ Hugging Face {resp.status_code}\nМодель: {HF_IMAGE_MODEL}\nURL: {url}\n\n{snippet}")

    except Exception:
        traceback.print_exc()
        message.reply_text("Ошибка при генерации изображения 🎨")

# -------- ГРАЦИОЗНОЕ ЗАВЕРШЕНИЕ --------
def _graceful_exit(sig, frame):
    logging.getLogger().info("Stop signal received (%s). Exiting...", sig)
    try:
        app.stop()
    finally:
        os._exit(0)

signal.signal(signal.SIGTERM, _graceful_exit)
signal.signal(signal.SIGINT, _graceful_exit)

# -------- ЗАПУСК --------
if __name__ == "__main__":
    try:
        log.info("✅ Бот запускается...")
        app.run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)













