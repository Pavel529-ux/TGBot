from pyrogram import Client, filters
from dotenv import load_dotenv
import os
import requests
import traceback

# Загружаем переменные из .env
load_dotenv()

# Получаем токены из окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Инициализируем Telegram клиента
app = Client("my_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# Обработка команды /start
@app.on_message(filters.command("start"))
def start_handler(client, message):
    message.reply_text("Привет! Я бот с интеллектом через OpenRouter 🤖")

# Обработка текстовых сообщений
@app.on_message(filters.text & ~filters.command("start"))
def text_handler(client_tg, message):
    user_text = message.text

    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": "openai/gpt-oss-120b",  # БЕСПЛАТНАЯ модель
            "messages": [
                {"role": "system", "content": "Ты — дружелюбный Telegram-бот."},
                {"role": "user", "content": user_text}
            ]
        }

        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload
        )

        print("STATUS:", response.status_code)
        print("RESPONSE TEXT:", response.text)

        response.raise_for_status()  # выбрасывает ошибку, если не 2xx

        reply = response.json()["choices"][0]["message"]["content"]
        message.reply_text(reply)

    except Exception as e:
        print("❌ Ошибка OpenRouter:")
        traceback.print_exc()
        message.reply_text("Произошла ошибка при общении с OpenRouter 🤖")

# Запуск бота
app.run()







