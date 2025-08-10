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
import signal

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ---------- ОКРУЖЕНИЕ ----------
load_dotenv()

BOT_TOKEN          = os.getenv("BOT_TOKEN")
API_ID_STR         = os.getenv("API_ID")
API_HASH           = os.getenv("API_HASH")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
STABILITY_API_KEY  = os.getenv("STABILITY_API_KEY")

# безопасный диагностический вывод (только префикс и длина — можно потом удалить)
log.info("OR key: %s... (len=%d)", (OPENROUTER_API_KEY or "")[:10], len(OPENROUTER_API_KEY or 0))
log.info("SDXL key: %s... (len=%d)", (STABILITY_API_KEY  or "")[:10], len(STABILITY_API_KEY  or 0))

missing = [k for k, v in {
    "BOT_TOKEN": BOT_TOKEN,
    "API_ID": API_ID_STR,
    "API_HASH": API_HASH,
    "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
    "STABILITY_API_KEY": STABILITY_API_KEY,
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
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "HTTP-Referer": "https://openrouter.ai",
        "X-Title": title,
    }

# ---------- SIGTERM ----------
def _graceful_shutdown(*_):
    log.info("🛑 Получен SIGTERM — завершаюсь.")
    sys.exit(0)
signal.signal(signal.SIGTERM, _graceful_shutdown)

# ---------- PYROGRAM ----------
app = Client("my_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# ---------- КОМАНДЫ ----------
@app.on_message(filters.command("start"))
def start_handler(_, message):
    uid = message.from_user.id
    chat_history[uid] = []
    message.reply_text(
        "Привет! Я бот с памятью и генерацией картинок 🤖\n"
        "— Просто пиши: я учитываю последние 10 реплик.\n"
        "— Картинка: /img кот в космосе, неон, 4k\n"
        "— Очистить память: /reset"
    )

@app.on_message(filters.command("reset"))
def reset_handler(_, message):
    uid = message.from_user.id
    chat_history[uid] = []
    message.reply_text("🧹 Память очищена!")

# ---------- ТЕКСТ (OpenRouter) ----------
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

        log.info("TEXT %s | %s", resp.status_code, resp.headers.get("content-type"))
        if resp.status_code != 200:
            message.reply_text(f"❌ OpenRouter {resp.status_code}:\n{resp.text[:500]}")
            return

        data = resp.json()
        bot_reply = data["choices"][0]["message"]["content"].strip()

        chat_history[uid].append({"role": "assistant", "content": bot_reply})
        chat_history[uid] = clamp_history(chat_history[uid])

        message.reply_text(bot_reply or "🤖 (пустой ответ)")

    except Exception:
        traceback.print_exc()
        message.reply_text("Произошла ошибка при общении с OpenRouter 🤖")

# ---------- КАРТИНКИ (Stability SDXL) ----------
@app.on_message(filters.command("img"))
def image_handler(_, message):
    prompt = " ".join(message.command[1:]).strip()
    if not prompt:
        message.reply_text("Напиши описание после команды, пример:\n/img кот в космосе, неон, 4k")
        return

    try:
        url = "https://api.stability.ai/v1/generation/stable-diffusion-xl-1024-v1-0/text-to-image"
        headers = {
            "Authorization": f"Bearer {STABILITY_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = {
            "text_prompts": [{"text": prompt}],
            "cfg_scale": 7,
            "height": 1024,
            "width": 1024,
            "samples": 1,
            "steps": 30
        }

        resp = requests.post(url, headers=headers, json=body, timeout=120)
        log.info("SDXL %s | %s", resp.status_code, resp.headers.get("content-type"))

        if resp.status_code != 200:
            message.reply_text(f"❌ Stability AI {resp.status_code}:\n{resp.text[:500]}")
            return

        data = resp.json()
        artifact = (data.get("artifacts") or [{}])[0]
        b64 = artifact.get("base64")
        if not b64:
            message.reply_text("Не удалось получить изображение из ответа Stability 😕")
            return

        img_bytes = base64.b64decode(b64)
        bio = BytesIO(img_bytes)
        bio.name = "image.png"
        message.reply_photo(bio, caption=f"🎨 По запросу: {prompt}")

    except Exception:
        traceback.print_exc()
        message.reply_text("Ошибка при генерации изображения 🎨")

# ---------- ЗАПУСК ----------
if __name__ == "__main__":
    try:
        log.info("✅ Бот запускается...")
        app.run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)












