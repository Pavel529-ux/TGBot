from pyrogram import Client, filters
from dotenv import load_dotenv
import os
import sys
import requests
import traceback
from collections import defaultdict
import base64
from io import BytesIO
import logging

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ---------- ОКРУЖЕНИЕ ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID_STR = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

missing = [k for k, v in {
    "BOT_TOKEN": BOT_TOKEN,
    "API_ID": API_ID_STR,
    "API_HASH": API_HASH,
    "OPENROUTER_API_KEY": OPENROUTER_API_KEY
}.items() if not v]
if missing:
    log.error("❌ Не заданы переменные окружения: %s", ", ".join(missing))
    sys.exit(1)

try:
    API_ID = int(API_ID_STR)
except Exception:
    log.error("❌ API_ID должен быть числом, получено: %r", API_ID_STR)
    sys.exit(1)

# ---------- ПАМЯТЬ ДИАЛОГА ----------
chat_history = defaultdict(list)
HISTORY_LIMIT = 10

def clamp_history(history):
    return history[-HISTORY_LIMIT:] if len(history) > HISTORY_LIMIT else history

def or_headers(title: str = "TelegramBot"):
    # Минимально достаточные заголовки для OpenRouter (OpenAI-совместимый API)
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json", 
        "HTTP-Referer": "https://openrouter.ai",
        "X-Title": title,
    }

# ---------- PYROGRAM ----------
import signal
def _graceful_shutdown(*_):
    log.info("🛑 Получен SIGTERM — завершаюсь (инициировано платформой).")
    sys.exit(0)
signal.signal(signal.SIGTERM, _graceful_shutdown)

app = Client("my_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# ---------- КОМАНДЫ ----------
@app.on_message(filters.command("start"))
def start_handler(_, message):
    uid = message.from_user.id
    chat_history[uid] = []
    message.reply_text(
        "Привет! Я бот с памятью 🤖\n"
        "— Пиши сообщения: я учитываю контекст последних 10 реплик.\n"
        "— Сгенерировать картинку: /img кот в космосе\n"
        "— Очистить память: /reset"
    )

@app.on_message(filters.command("reset"))
def reset_handler(_, message):
    uid = message.from_user.id
    chat_history[uid] = []
    message.reply_text("🧹 Память очищена!")

# ---------- ТЕКСТ С ПАМЯТЬЮ ----------
@app.on_message(filters.text & ~filters.command(["start", "reset", "img"]))
def text_handler(_, message):
    uid = message.from_user.id
    user_text = message.text

    chat_history[uid].append({"role": "user", "content": user_text})
    chat_history[uid] = clamp_history(chat_history[uid])

    try:
        payload = {
            "model": "openai/gpt-oss-120b",  # бесплатная текстовая модель
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

        log.info("TEXT STATUS: %s", resp.status_code)
        log.info("TEXT RESP: %s", resp.text[:600])

        if resp.status_code != 200:
            message.reply_text(f"❌ OpenRouter (text) {resp.status_code}:\n{resp.text}")
            return

        data = resp.json()
        bot_reply = data["choices"][0]["message"]["content"].strip()

        chat_history[uid].append({"role": "assistant", "content": bot_reply})
        chat_history[uid] = clamp_history(chat_history[uid])

        message.reply_text(bot_reply or "🤖 (пустой ответ)")

    except Exception:
        traceback.print_exc()
        message.reply_text("Произошла ошибка при общении с OpenRouter 🤖")

# ---------- КАРТИНКИ /img ----------
@app.on_message(filters.command("img"))
def image_handler(_, message):
    prompt = " ".join(message.command[1:]).strip()
    if not prompt:
        message.reply_text("Напиши описание после команды, пример:\n/img кот в космосе, неон, 4k")
        return

    try:
        img_payload = {
            "model": "stabilityai/stable-diffusion-xl",
            "prompt": prompt,
            "size": "1024x1024",
        }

        # OpenAI-совместимый image-эндпоинт у OpenRouter
        resp = requests.post(
            "https://openrouter.ai/api/v1/images/generations",
            headers=or_headers("TelegramBotImages"),
            json=img_payload,
            timeout=120,
            allow_redirects=False
        )

        log.info("IMG STATUS: %s", resp.status_code)
        log.info("IMG TEXT: %s", resp.text[:1000])

        if resp.status_code != 200:
            message.reply_text(f"❌ OpenRouter (image) {resp.status_code}:\n{resp.text}")
            return

        data = resp.json()
        item = None
        if isinstance(data, dict) and "data" in data and data["data"]:
            item = data["data"][0]

        if not item:
            message.reply_text("Не удалось получить данные изображения из ответа API 😕")
            return

        if "url" in item and item["url"]:
            message.reply_photo(item["url"], caption=f"🎨 По запросу: {prompt}")
            return

        if "b64_json" in item and item["b64_json"]:
            try:
                img_bytes = base64.b64decode(item["b64_json"])
                bio = BytesIO(img_bytes)
                bio.name = "image.png"
                message.reply_photo(bio, caption=f"🎨 По запросу: {prompt}")
                return
            except Exception:
                traceback.print_exc()
                message.reply_text("Получил base64, но не смог декодировать изображение 😕")
                return

        message.reply_text("API вернул неожиданный формат данных для изображения 😕")

    except Exception:
        traceback.print_exc()
        message.reply_text("Ошибка при генерации изображения 🎨")

# ---------- ЗАПУСК ----------
if __name__ == "__main__":
    try:
        log.info("✅ Бот запускается...")
        app.run()  # блокирует поток и держит контейнер живым
    except Exception:
        traceback.print_exc()
        # Если что-то пошло не так, не гасим контейнер молча
        sys.exit(1)










