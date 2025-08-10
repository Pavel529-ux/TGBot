from pyrogram import Client, filters
from dotenv import load_dotenv
import os
import requests
import traceback
from collections import defaultdict

# ==== загрузка переменных окружения ====
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# быстрая валидация, чтобы не падать молча
for k, v in {"BOT_TOKEN": BOT_TOKEN, "API_ID": API_ID, "API_HASH": API_HASH, "OPENROUTER_API_KEY": OPENROUTER_API_KEY}.items():
    if not v:
        print(f"⚠️ Переменная окружения {k} не задана")
print("✅ Бот запускается...")

# ==== память диалога ====
# {user_id: [ {role: "user"/"assistant"/"system", content: "..."}, ... ]}
chat_history = defaultdict(list)
HISTORY_LIMIT = 10

# ==== инициализация Pyrogram ====
app = Client("my_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# ==== помощники ====
def or_headers(title: str = "TelegramBot"):
    # Минимально необходимые заголовки; Referer/Title полезны для аналитики, но не обязательны
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://openrouter.ai",
        "X-Title": title,
    }

def clamp_history(history):
    if len(history) > HISTORY_LIMIT:
        return history[-HISTORY_LIMIT:]
    return history

# ==== команды ====
@app.on_message(filters.command("start"))
def start_handler(client, message):
    uid = message.from_user.id
    chat_history[uid] = []
    message.reply_text(
        "Привет! Я бот с памятью 🤖\n"
        "— Пиши сообщения, я буду помнить контекст (до 10 реплик).\n"
        "— Сгенерировать картинку: /img кот в космосе\n"
        "— Очистить память: /reset"
    )

@app.on_message(filters.command("reset"))
def reset_handler(client, message):
    uid = message.from_user.id
    chat_history[uid] = []
    message.reply_text("🧹 Память очищена!")

# ==== текстовые ответы с памятью ====
@app.on_message(filters.text & ~filters.command(["start", "reset", "img"]))
def text_handler(client_tg, message):
    uid = message.from_user.id
    user_text = message.text

    # добавляем реплику пользователя в память
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

        # ВНИМАНИЕ: чат-эндпоинт — /v1/chat/completions
        resp = requests.post(
            "https://openrouter.ai/v1/chat/completions",
            headers=or_headers("TelegramBotWithMemory"),
            json=payload,
            timeout=60,
        )

        print("TEXT STATUS:", resp.status_code)
        print("TEXT RESP:", resp.text[:500])  # не шумим в логах

        if resp.status_code != 200:
            message.reply_text(f"❌ OpenRouter (text) {resp.status_code}:\n{resp.text}")
            return

        data = resp.json()
        bot_reply = data["choices"][0]["message"]["content"].strip()

        # сохраняем ответ ассистента в память
        chat_history[uid].append({"role": "assistant", "content": bot_reply})
        chat_history[uid] = clamp_history(chat_history[uid])

        message.reply_text(bot_reply or "🤖 (пустой ответ)")

    except Exception:
        traceback.print_exc()
        message.reply_text("Произошла ошибка при общении с OpenRouter 🤖")

# ==== генерация изображений ====
@app.on_message(filters.command("img"))
def image_handler(client_tg, message):
    prompt = " ".join(message.command[1:]).strip()
    if not prompt:
        message.reply_text("Напиши описание после команды, пример:\n/img кот в космосе, неон, 4k")
        return

    try:
        # Для изображений используем специальный generate-эндпоинт OpenRouter
        img_payload = {
            "model": "stabilityai/stable-diffusion-xl",
            "prompt": prompt,
            # дополнительные параметры по желанию:
            # "size": "1024x1024",
            # "num_images": 1,
            # "steps": 28,
            # "cfg_scale": 7.5,
        }

        resp = requests.post(
            "https://openrouter.ai/api/v1/generate",
            headers=or_headers("TelegramBotImages"),
            json=img_payload,
            timeout=120,
        )

        print("IMG STATUS:", resp.status_code)
        print("IMG RESP:", resp.text[:500])

        if resp.status_code != 200:
            message.reply_text(f"❌ OpenRouter (image) {resp.status_code}:\n{resp.text}")
            return

        data = resp.json()
        # у разных провайдеров структура может отличаться; чаще всего есть data[0].url
        try:
            img_url = data["data"][0]["url"]
        except Exception:
            # fallback: попробуем распространённые варианты
            img_url = (
                data.get("output", [{}])[0].get("url") or
                data.get("images", [{}])[0].get("url")
            )

        if not img_url:
            message.reply_text("Не удалось получить URL изображения из ответа API 😕")
            return

        message.reply_photo(img_url, caption=f"🎨 По запросу: {prompt}")

    except Exception:
        traceback.print_exc()
        message.reply_text("Ошибка при генерации изображения 🎨")

# ==== запуск ====
print("✅ Готов к работе. Ожидаю сообщения...")
app.run()









