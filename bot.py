from pyrogram import Client, filters
from dotenv import load_dotenv
import os
import requests
import traceback
from collections import defaultdict

# Загружаем переменные
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Хранилище истории сообщений: {user_id: [ {role, content}, ... ]}
chat_history = defaultdict(list)

# Инициализация бота
app = Client("my_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

@app.on_message(filters.command("start"))
def start_handler(client, message):
    user_id = message.from_user.id
    chat_history[user_id] = []  # очищаем историю при /start
    message.reply_text("Привет! Я бот с памятью 🤖 Теперь я буду помнить наш разговор.")

@app.on_message(filters.text & ~filters.command("start"))
def text_handler(client_tg, message):
    user_id = message.from_user.id
    user_text = message.text

    # Добавляем сообщение пользователя в историю
    chat_history[user_id].append({"role": "user", "content": user_text})

    # Ограничиваем историю до последних 10 сообщений
    if len(chat_history[user_id]) > 10:
        chat_history[user_id] = chat_history[user_id][-10:]

    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://openrouter.ai",
            "X-Title": "TelegramBotWithMemory"
        }

        payload = {
            "model": "openai/gpt-oss-120b",  # бесплатная модель
            "messages": [
                {"role": "system", "content": "Ты — дружелюбный Telegram-бот, отвечай коротко и по делу."}
            ] + chat_history[user_id]
        }

        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload
        )
        response.raise_for_status()

        bot_reply = response.json()["choices"][0]["message"]["content"]

        # Добавляем ответ бота в историю
        chat_history[user_id].append({"role": "assistant", "content": bot_reply})

        message.reply_text(bot_reply)

    except Exception as e:
        print("❌ Ошибка OpenRouter:")
        traceback.print_exc()
        message.reply_text("Произошла ошибка при общении с OpenRouter 🤖")

app.run()








