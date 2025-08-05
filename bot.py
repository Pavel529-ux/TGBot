from pyrogram import Client, filters
from dotenv import load_dotenv
import os
import requests
import traceback

print("✅ Bot is starting up...")


# Загружаем переменные из .env или среды (например, Railway)
load_dotenv()

# Получаем токены из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Инициализируем Telegram клиента
app = Client("my_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# Обработка команды /start
@app.on_message(filters.command("start"))
def start_handler(client, message):
    message.reply_text("Привет! Я бот с интеллектом ChatGPT через OpenRouter 🤖")

# Обработка обычных текстовых сообщений
@app.on_message(filters.text & ~filters.command("start"))
def text_handler(client_tg, message):
    user_text = message.text

    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://openrouter.ai",
            "X-Title": "My Telegram Bot"
        }

        payload = {
            "model": "openai/gpt-3.5-turbo",
            "messages": [
                {"role": "system", "content": "Ты — дружелюбный Telegram-бот."},
                {"role": "user", "content": user_text}
            ]
        }

        # ✅ Новый корректный URL:
        response = requests.post(
            "https://openrouter.ai/v1/chat/completions",  # ← исправлено
            headers=headers,
            json=payload
        )
        response.raise_for_status()

        reply = response.json()["choices"][0]["message"]["content"]
        message.reply_text(reply)

    except Exception as e:
        print("❌ Ошибка OpenRouter:")
        traceback.print_exc()
        message.reply_text("Произошла ошибка при общении с OpenRouter 🤖")

# Запуск бота
print("✅ Готов к работе. Ожидаю сообщения...")
app.run()




