from pyrogram import Client, filters
from dotenv import load_dotenv
import os
import requests
import traceback
from collections import defaultdict
import base64
from io import BytesIO

# ==== ЗАГРУЗКА ПЕРЕМЕННЫХ ====
# На Railway .env можно не использовать, но локально это удобно.
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Быстрая проверка переменных
for k, v in {
    "BOT_TOKEN": BOT_TOKEN,
    "API_ID": API_ID,
    "API_HASH": API_HASH,
    "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
}.items():
    if not v:
        print(f"⚠️ Переменная окружения {k} не задана")

print("✅ Бот запускается...")

# ==== ПАМЯТЬ ДИАЛОГА ====
# {user_id: [ {role, content}, ... ]}
chat_history = defaultdict(list)
HISTORY_LIMIT = 10

def clamp_history(history):
    if len(history) > HISTORY_LIMIT:
        return history[-HISTORY_LIMIT:]
    return history

# ==== ЗАГОЛОВКИ ДЛЯ OPENROUTER ====
def or_headers(title: str = "TelegramBot"):
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        # Referer/Title не обязательны, но помогают аналитике
        "HTTP-Referer": "https://openrouter.ai",
        "X-Title": title,
    }

# ==== PYROGRAM APP ====
app = Client("my_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# ==== КОМАНДЫ ====
@app.on_message(filters.command("start"))
def start_handler(client, message):
    uid = message.from_user.id
    chat_history[uid] = []
    message.reply_text(
        "Привет! Я бот с памятью 🤖\n"
        "— Пиши сообщения: я учитываю контекст последних 10 реплик.\n"
        "— Сгенерировать картинку: /img кот в космосе\n"
        "— Очистить память: /reset"
    )

@app.on_message(filters.command("reset"))
def reset_handler(client, message):
    uid = message.from_user.id
    chat_history[uid] = []
    message.reply_text("🧹 Память очищена!")

# ==== ТЕКСТ: ОТВЕТЫ С ПАМЯТЬЮ ====
@app.on_message(filters.text & ~filters.command(["start", "reset", "img"]))
def text_handler(client_tg, message):
    uid = message.from_user.id
    user_text = message.text

    # добавляем реплику пользователя
    chat_history[uid].append({"role": "user", "content": user_text})
    chat_history[uid] = clamp_history(chat_history[uid])

    try:
        payload = {
            "model": "openai/gpt-oss-120b",  # бесплатная текстовая модель через OpenRouter
            "messages": [
                {"role": "system", "content": "Ты — дружелюбный Telegram-бот. Отвечай кратко и по делу."},
                *chat_history[uid],
            ],
        }

        # Чат-эндпоинт (OpenAI-совместимый)
        resp = requests.post(
            "https://openrouter.ai/v1/chat/completions",
            headers=or_headers("TelegramBotWithMemory"),
            json=payload,
            timeout=60,
        )

        print("TEXT STATUS:", resp.status_code)
        print("TEXT RESP:", resp.text[:500])

        if resp.status_code != 200:
            message.reply_text(f"❌ OpenRouter (text) {resp.status_code}:\n{resp.text}")
            return

        data = resp.json()
        bot_reply = data["choices"][0]["message"]["content"].strip()

        # сохраняем ответ ассистента
        chat_history[uid].append({"role": "assistant", "content": bot_reply})
        chat_history[uid] = clamp_history(chat_history[uid])

        message.reply_text(bot_reply or "🤖 (пустой ответ)")

    except Exception:
        traceback.print_exc()
        message.reply_text("Произошла ошибка при общении с OpenRouter 🤖")

# ==== КАРТИНКИ: /img ====
@app.on_message(filters.command("img"))
def image_handler(client_tg, message):
    prompt = " ".join(message.command[1:]).strip()
    if not prompt:
        message.reply_text("Напиши описание после команды, пример:\n/img кот в космосе, неон, 4k")
        return

    try:
        img_payload = {
            "model": "stabilityai/stable-diffusion-xl",
            "prompt": prompt,
            "size": "1024x1024",  # можно 512x512 / 768x768 / 1024x1024
            # "n": 1,            # если провайдер поддерживает несколько изображений
        }

        # Эндпоинт генерации изображений (OpenAI-совместимый у OpenRouter)
        resp = requests.post(
            "https://openrouter.ai/v1/images/generations",
            headers=or_headers("TelegramBotImages"),
            json=img_payload,
            timeout=120,
        )

        print("IMG STATUS:", resp.status_code)
        print("IMG TEXT:", resp.text[:1200])

        if resp.status_code != 200:
            message.reply_text(f"❌ OpenRouter (image) {resp.status_code}:\n{resp.text}")
            return

        data = resp.json()

        # Пытаемся вытащить url или base64
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

# ==== ЗАПУСК ====
print("✅ Готов к работе. Ожидаю сообщения...")
app.run()










