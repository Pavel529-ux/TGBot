from pyrogram import Client, filters
from dotenv import load_dotenv
import os
import logging
import sys

# === Загрузка переменных окружения ===
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_HASH = os.getenv("API_HASH")
API_ID_STR = os.getenv("API_ID")

# Простая валидация, чтобы в логах было понятно, если что-то не так
missing = [k for k, v in [("BOT_TOKEN", BOT_TOKEN), ("API_HASH", API_HASH), ("API_ID", API_ID_STR)] if not v]
if missing:
    print(f"❌ Не заданы переменные: {', '.join(missing)}")
    sys.exit(1)

try:
    API_ID = int(API_ID_STR)
except ValueError:
    print(f"❌ API_ID должен быть числом, а получено: {API_ID_STR!r}")
    sys.exit(1)

# Логи по-умолчанию
logging.basicConfig(level=logging.INFO)

# === Инициализация бота ===
app = Client("my_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# /start
@app.on_message(filters.command("start"))
def start_handler(client, message):
    message.reply_text("Привет! ✅ Бот запущен и жив. Напиши любой текст — отвечу 'Я жив!'")

# Любой текст (кроме /start)
@app.on_message(filters.text & ~filters.command("start"))
def alive_handler(client, message):
    message.reply_text("Я жив! ✅")

if __name__ == "__main__":
    print("✅ Мини-бот запускается...")
    app.run()
    print("🛑 Мини-бот остановлен")










