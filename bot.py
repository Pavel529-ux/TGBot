# bot.py
from pyrogram import Client, filters
from dotenv import load_dotenv
import os
import sys
import re
import requests
import traceback
from collections import defaultdict
from io import BytesIO
import logging
import signal

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("bot")

# ---------- ОКРУЖЕНИЕ ----------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID_STR = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

# OpenRouter (текст + перевод)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
# бесплатная/дешевая текстовая модель для чата/перевода
OR_MODEL = os.getenv("OR_TEXT_MODEL", "openai/gpt-oss-120b")

# Hugging Face (картинки)
HF_TOKEN = os.getenv("HF_TOKEN")
# рабочая публичная модель; можно менять через переменные Railway
HF_IMAGE_MODEL = os.getenv("HF_IMAGE_MODEL", "stabilityai/sdxl-turbo")

missing = [k for k, v in {
    "BOT_TOKEN": BOT_TOKEN,
    "API_ID": API_ID_STR,
    "API_HASH": API_HASH,
    "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
    "HF_TOKEN": HF_TOKEN,
}.items() if not v]
if missing:
    log.error("❌ Не заданы переменные окружения: %s", ", ".join(missing))
    sys.exit(1)

try:
    API_ID = int(API_ID_STR)
except Exception:
    log.error("❌ API_ID должен быть числом, получено: %r", API_ID_STR)
    sys.exit(1)

# ---------- УТИЛИТЫ ----------
def or_headers(title: str = "TelegramBot"):
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "HTTP-Referer": "https://openrouter.ai",
        "X-Title": title,
    }

def has_cyrillic(text: str) -> bool:
    return bool(re.search(r"[\u0400-\u04FF]", text))

def translate_to_english(text: str) -> str:
    """
    Перевод через OpenRouter без воды — только готовая фраза.
    """
    try:
        payload = {
            "model": OR_MODEL,
            "messages": [
                {"role": "system", "content": (
                    "You are a precise translator. "
                    "Translate the user prompt from Russian to concise English. "
                    "Return ONLY the translated text, no explanations."
                )},
                {"role": "user", "content": text}
            ],
            "temperature": 0.2
        }
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=or_headers("PromptTranslator"),
            json=payload,
            timeout=40,
            allow_redirects=False
        )
        if r.status_code == 200 and r.headers.get("content-type","").startswith("application/json"):
            return r.json()["choices"][0]["message"]["content"].strip()
        log.warning("Translate: %s | %s", r.status_code, r.text[:300])
    except Exception:
        traceback.print_exc()
    # если что-то пошло не так — вернём исходный
    return text

def boost_prompt(en_prompt: str, user_negative: str = "") -> tuple[str, str]:
    """
    Усиливаем промпт качественными токенами.
    Возвращаем (positive_prompt, negative_prompt)
    """
    base_positive = (
        f"{en_prompt}, ultra-detailed, high quality, high resolution, "
        f"sharp focus, intricate details, 8k, dramatic lighting"
    )
    base_negative = (
        "lowres, blurry, out of focus, pixelated, deformed, bad anatomy, "
        "extra fingers, watermark, text, signature"
    )
    # добавим пользовательский negative (если есть)
    neg = (base_negative + ", " + user_negative) if user_negative else base_negative
    return base_positive, neg

# ---------- ПАМЯТЬ ДИАЛОГА ----------
chat_history = defaultdict(list)
HISTORY_LIMIT = 10

def clamp_history(history):
    return history[-HISTORY_LIMIT:] if len(history) > HISTORY_LIMIT else history

# ---------- PYROGRAM ----------
app = Client("my_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# ---------- КОМАНДЫ ----------
@app.on_message(filters.command("start"))
def start_handler(_, message):
    uid = message.from_user.id
    chat_history[uid] = []
    message.reply_text(
        "Привет! Я бот с памятью 🤖\n"
        "— Пиши сообщения: учитываю контекст последних 10 реплик.\n"
        "— Картинка: /img кот в космосе  (поддерживает  --no ...  для исключений)\n"
        "— Очистить память: /reset"
    )

@app.on_message(filters.command("reset"))
def reset_handler(_, message):
    uid = message.from_user.id
    chat_history[uid] = []
    message.reply_text("🧹 Память очищена!")

# ---------- ТЕКСТ С ПАМЯТЬЮ (OpenRouter) ----------
@app.on_message(filters.text & ~filters.command(["start", "reset", "img"]))
def text_handler(_, message):
    uid = message.from_user.id
    user_text = message.text

    chat_history[uid].append({"role": "user", "content": user_text})
    chat_history[uid] = clamp_history(chat_history[uid])

    try:
        payload = {
            "model": OR_MODEL,
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

# ---------- КАРТИНКИ /img (HF Inference API + перевод + буст) ----------
@app.on_message(filters.command("img"))
def image_handler(_, message):
    """
    Поддержка:
    /img кот в космосе --no текст, подписи
    """
    raw = " ".join(message.command[1:]).strip()
    if not raw:
        message.reply_text(
            "Напиши описание после команды, например:\n"
            "/img кот в космосе, неон, 4k  --no текст, подписи"
        )
        return

    # разбор пользовательского negative после "--no"
    user_neg = ""
    if "--no" in raw:
        parts = raw.split("--no", 1)
        raw = parts[0].strip()
        user_neg = parts[1].strip()

    # перевод на английский при наличии кириллицы
    prompt_src = raw
    prompt_en = translate_to_english(raw) if has_cyrillic(raw) else raw

    # буст промпта
    pos_prompt, neg_prompt = boost_prompt(prompt_en, user_negative=user_neg)

    try:
        url = f"https://api-inference.huggingface.co/models/{HF_IMAGE_MODEL}"
        headers = {
            "Authorization": f"Bearer {HF_TOKEN}",
            "Accept": "image/png"
        }
        # многие t2i модели понимают параметры ниже (могут игнорировать)
        payload = {
            "inputs": pos_prompt,
            "parameters": {
                "negative_prompt": neg_prompt,
                "num_inference_steps": 24,
                "guidance_scale": 7.0
            },
            "options": {"wait_for_model": True}
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=180)
        ct = resp.headers.get("content-type", "")
        log.info("IMG %s | %s", resp.status_code, ct)

        if resp.status_code == 200 and ct.startswith("image/"):
            bio = BytesIO(resp.content)
            bio.name = "image.png"
            # покажем исходный русский (если был) для удобства
            shown_prompt = prompt_src if prompt_src else prompt_en
            message.reply_photo(bio, caption=f"🎨 По запросу: {shown_prompt}")
            return

        snippet = (resp.text or "")[:800]
        message.reply_text(
            "❌ Hugging Face {code}\nМодель: {model}\nURL: {url}\n\n{snippet}".format(
                code=resp.status_code, model=HF_IMAGE_MODEL, url=url, snippet=snippet
            )
        )

    except Exception:
        traceback.print_exc()
        message.reply_text("Ошибка при генерации изображения 🎨")

# ---------- ГРАЦИОЗНОЕ ЗАВЕРШЕНИЕ ----------
def _graceful_exit(sig, frame):
    logging.getLogger().info("Stop signal received (%s). Exiting...", sig)
    try:
        app.stop()
    finally:
        os._exit(0)

signal.signal(signal.SIGTERM, _graceful_exit)
signal.signal(signal.SIGINT, _graceful_exit)

# ---------- ЗАПУСК ----------
if __name__ == "__main__":
    try:
        log.info("✅ Бот запускается...")
        app.run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)














