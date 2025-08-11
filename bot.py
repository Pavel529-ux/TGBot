# bot.py
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    InlineQueryResultPhoto, InlineQueryResultArticle, InputTextMessageContent
)
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
import threading
from datetime import datetime, timedelta

# ==========================
# ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ (Railway → Variables)
# ОБЯЗАТЕЛЬНО:
# BOT_TOKEN
# API_ID
# API_HASH
# OPENROUTER_API_KEY
# HF_TOKEN
#
# ДЛЯ КАТАЛОГА (1С/файл/URL):
# CATALOG_URL
# CATALOG_AUTH_USER
# CATALOG_AUTH_PASS
# CATALOG_REFRESH_MIN=30
# TELEGRAM_ADMIN_ID
# MANAGER_CHAT_ID
#
# ОПЦИОНАЛЬНО:
# OR_TEXT_MODEL
# HF_IMAGE_MODEL
# ==========================

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("bot")

# ---------- ОКРУЖЕНИЕ ----------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID_STR = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

# Текст (OpenRouter)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OR_MODEL = os.getenv("OR_TEXT_MODEL", "openai/gpt-oss-120b")

# Картинки (Hugging Face)
HF_TOKEN = os.getenv("HF_TOKEN")
HF_IMAGE_MODEL = os.getenv("HF_IMAGE_MODEL", "stabilityai/sdxl-turbo")

# Каталог / 1С
CATALOG_URL = os.getenv("CATALOG_URL")
CATALOG_AUTH_USER = os.getenv("CATALOG_AUTH_USER")
CATALOG_AUTH_PASS = os.getenv("CATALOG_AUTH_PASS")
CATALOG_REFRESH_MIN = int(os.getenv("CATALOG_REFRESH_MIN", "30"))
TELEGRAM_ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
MANAGER_CHAT_ID = int(os.getenv("MANAGER_CHAT_ID", "0"))

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
    return bool(re.search(r"[\u0400-\u04FF]", text or ""))

def translate_to_english(text: str) -> str:
    """RU → EN через OpenRouter. Возвращает только перевод."""
    try:
        payload = {
            "model": OR_MODEL,
            "messages": [
                {"role": "system", "content": (
                    "You are a precise translator. Translate the user prompt "
                    "from Russian to concise English. Return ONLY the translated text."
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
        log.warning("Translate HTTP %s | %s", r.status_code, r.text[:300])
    except Exception:
        traceback.print_exc()
    return text  # fallback

def boost_prompt(en_prompt: str, user_negative: str = "") -> tuple[str, str]:
    """Возвращает (positive_prompt, negative_prompt) с усилением качества."""
    base_positive = (
        f"{en_prompt}, ultra-detailed, high quality, high resolution, "
        f"sharp focus, intricate details, 8k, dramatic lighting"
    )
    base_negative = (
        "lowres, blurry, out of focus, pixelated, deformed, bad anatomy, "
        "extra fingers, watermark, text, signature"
    )
    neg = (base_negative + ", " + user_negative) if user_negative else base_negative
    return base_positive, neg

PHONE_RE = re.compile(r"^\+?\d[\d\s\-()]{6,}$")

# ---------- ПАМЯТЬ ДИАЛОГА ----------
chat_history = defaultdict(list)
HISTORY_LIMIT = 10

def clamp_history(history):
    return history[-HISTORY_LIMIT:] if len(history) > HISTORY_LIMIT else history

# ---------- КАТАЛОГ ----------
catalog = []             # список dict'ов товаров
catalog_last_fetch = None
catalog_lock = threading.Lock()
pending_reserve = {}     # user_id -> product_id (ожидаем телефон)

def fetch_catalog(force=False):
    """Загрузить каталог из CATALOG_URL (JSON список). Вернёт True при успешном обновлении."""
    global catalog, catalog_last_fetch
    with catalog_lock:
        now = datetime.utcnow()
        if not force and catalog_last_fetch and now - catalog_last_fetch < timedelta(minutes=CATALOG_REFRESH_MIN):
            return False
        if not CATALOG_URL:
            log.warning("CATALOG_URL не задан — пропускаю загрузку каталога")
            return False

        auth = (CATALOG_AUTH_USER, CATALOG_AUTH_PASS) if CATALOG_AUTH_USER else None
        try:
            r = requests.get(CATALOG_URL, auth=auth, timeout=30)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                catalog = data
                catalog_last_fetch = now
                log.info("Каталог обновлён: %d позиций", len(catalog))
                return True
            else:
                log.error("Неожиданный формат каталога (ожидался список)")
        except Exception as e:
            traceback.print_exc()
            log.error("Ошибка загрузки каталога: %s", e)
        return False

def periodic_refresh():
    try:
        fetch_catalog(force=False)
    finally:
        # единичный таймер; при рестарте процесса создаётся заново
        threading.Timer(CATALOG_REFRESH_MIN * 60, periodic_refresh).start()

def search_products(query, limit=10):
    q = (query or "").strip().lower()
    results = []
    for item in catalog:
        name = str(item.get("name","")).lower()
        sku  = str(item.get("sku","")).lower()
        brand = str(item.get("brand","")).lower()
        hay = f"{name} {sku} {brand}"
        if q in hay:
            results.append(item)
            if len(results) >= limit:
                break
    return results

def product_caption(p):
    price = p.get("price")
    stock = p.get("stock")
    lines = [
        f"🛒 {p.get('name','')}",
        f"Артикул: {p.get('sku','—')}",
        f"Цена: {price} ₽" if price is not None else "Цена: уточняйте",
        f"В наличии: {stock} шт." if stock is not None else "Наличие: уточняйте",
    ]
    return "\n".join(lines)

def product_keyboard(p):
    pid = p.get("id") or p.get("sku")
    buttons = [[InlineKeyboardButton("📝 Забронировать", callback_data=f"reserve:{pid}")]]
    if p.get("category"):
        buttons.append([InlineKeyboardButton(f"📂 Категория: {p['category']}", callback_data=f"cat:{p['category']}")])
    # подсказка поиска в инлайн-режиме
    buttons.append([InlineKeyboardButton("🔎 Искать в чате", switch_inline_query_current_chat=p.get("sku",""))])
    return InlineKeyboardMarkup(buttons)

# ---------- PYROGRAM ----------
app = Client("my_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# ---------- КОМАНДЫ / ОБРАБОТЧИКИ ----------

@app.on_message(filters.command("start") & filters.private)
def start_handler(_, message):
    uid = message.from_user.id
    chat_history[uid] = []
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Каталог", callback_data="cat:all"),
         InlineKeyboardButton("🔎 Поиск", switch_inline_query_current_chat="")],
        [InlineKeyboardButton("🧹 Сбросить контекст", callback_data="reset_ctx")]
    ])
    message.reply_text(
        "Привет! Я бот магазина электрооборудования ⚡\n"
        "— Диалог с памятью\n"
        "— Картинки: /img <описание>  (поддерживает  --no ...)\n"
        "— Каталог: /catalog, поиск: /find <запрос>\n"
        "— Очистить контекст: /reset",
        reply_markup=kb
    )

@app.on_message(filters.command("help") & filters.private)
def help_handler(_, message):
    message.reply_text(
        "Команды:\n"
        "• /catalog — показать позиции\n"
        "• /find <запрос> — поиск по названию/SKU/бренду\n"
        "• /img <описание> --no <исключить> — сгенерировать картинку\n"
        "• /reset — очистить контекст\n"
        "• /ping — проверить доступность\n"
    )

@app.on_message(filters.command("ping"))
def ping_handler(_, message):
    message.reply_text("pong ✅")

# ----- КАТАЛОГ -----
@app.on_message(filters.command("catalog"))
def catalog_handler(_, message):
    if not catalog:
        message.reply_text("Каталог пока пуст, попробуйте позже.")
        return
    for p in catalog[:10]:
        try:
            img = p.get("image_url")
            caption = product_caption(p)
            kb = product_keyboard(p)
            if img:
                message.reply_photo(img, caption=caption, reply_markup=kb)
            else:
                message.reply_text(caption, reply_markup=kb)
        except Exception:
            traceback.print_exc()

@app.on_message(filters.command("find"))
def find_handler(_, message):
    query = " ".join(message.command[1:]).strip()
    if not query:
        message.reply_text("Использование: /find <название или артикул>")
        return
    if not catalog:
        message.reply_text("Каталог пока не загружен.")
        return

    results = search_products(query, limit=10)
    if not results:
        message.reply_text("Ничего не нашлось 😕 Попробуй другой запрос.")
        return

    for p in results:
        try:
            img = p.get("image_url")
            caption = product_caption(p)
            kb = product_keyboard(p)
            if img:
                message.reply_photo(img, caption=caption, reply_markup=kb)
            else:
                message.reply_text(caption, reply_markup=kb)
        except Exception:
            traceback.print_exc()

@app.on_inline_query()
def inline_query_handler(client, inline_query):
    q = inline_query.query.strip()
    if not q or not catalog:
        return
    results = search_products(q, limit=25)

    items = []
    for idx, p in enumerate(results):
        caption = product_caption(p)
        kb = product_keyboard(p)
        img = p.get("image_url")

        if img:
            items.append(
                InlineQueryResultPhoto(
                    photo_url=img,
                    thumb_url=img,
                    caption=caption,
                    reply_markup=kb,
                    id=str(idx)
                )
            )
        else:
            items.append(
                InlineQueryResultArticle(
                    title=p.get("name","Товар"),
                    description=f"SKU: {p.get('sku','—')} | {p.get('price','—')} ₽",
                    input_message_content=InputTextMessageContent(caption),
                    reply_markup=kb,
                    id=str(idx)
                )
            )
    try:
        inline_query.answer(items, cache_time=5, is_personal=True)
    except Exception:
        traceback.print_exc()

@app.on_callback_query()
def callbacks_handler(client, cq):
    data = cq.data or ""
    if data == "reset_ctx":
        chat_history[cq.from_user.id] = []
        cq.answer("Контекст очищён")
        cq.message.reply_text("Контекст очищён. /start")
        return

    if data.startswith("reserve:"):
        pid = data.split(":",1)[1]
        pending_reserve[cq.from_user.id] = pid
        cq.message.reply_text("Оставьте, пожалуйста, номер телефона для связи:")
        cq.answer()
    elif data.startswith("cat:"):
        cat = data.split(":",1)[1].strip().lower()
        items = [p for p in catalog if cat in ("all", str(p.get("category","")).lower())]
        if not items:
            cq.message.reply_text("В этой категории пока пусто.")
            cq.answer()
            return
        for p in items[:10]:
            img = p.get("image_url")
            caption = product_caption(p)
            kb = product_keyboard(p)
            if img:
                cq.message.reply_photo(img, caption=caption, reply_markup=kb)
            else:
                cq.message.reply_text(caption, reply_markup=kb)
        cq.answer()

@app.on_message(filters.command("sync1c"))
def sync1c_handler(_, message):
    if TELEGRAM_ADMIN_ID and message.from_user.id != TELEGRAM_ADMIN_ID:
        message.reply_text("Недостаточно прав.")
        return
    ok = fetch_catalog(force=True)
    message.reply_text("✅ Каталог обновлён" if ok else "❌ Не удалось обновить каталог, проверь логи.")

# ----- СБОР ТЕЛЕФОНА ДЛЯ БРОНИ -----
@app.on_message(filters.text & ~filters.command(["start","reset","img","catalog","find","sync1c","help","ping"]))
def maybe_collect_phone(_, message):
    uid = message.from_user.id
    if uid in pending_reserve:
        pid = pending_reserve.get(uid)
        phone = (message.text or "").strip()

        if not PHONE_RE.match(phone):
            message.reply_text("Похоже, номер не распознан. Пример: +7 999 123-45-67\nОтправьте номер ещё раз.")
            return

        # найдём товар по pid
        product = None
        for p in catalog:
            if p.get("id") == pid or p.get("sku") == pid:
                product = p
                break

        text = (
            "🧾 Новая бронь:\n"
            f"Пользователь: @{message.from_user.username or message.from_user.id}\n"
            f"Телефон: {phone}\n"
            f"Товар: {product.get('name','') if product else pid}\n"
            f"SKU: {product.get('sku','—') if product else '—'}\n"
            f"Цена: {product.get('price','—') if product else '—'} ₽"
        )

        pending_reserve.pop(uid, None)

        if MANAGER_CHAT_ID:
            try:
                _.send_message(MANAGER_CHAT_ID, text)
            except Exception:
                traceback.print_exc()

        message.reply_text("Спасибо! Менеджер скоро свяжется для подтверждения 😊")
        return  # не пускаем дальше

# ----- КАРТИНКИ /img (HF Inference API + перевод + буст) -----
@app.on_message(filters.command("img"))
def image_handler(_, message):
    """
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
        model = (HF_IMAGE_MODEL or "stabilityai/sdxl-turbo").strip()
        url = f"https://api-inference.huggingface.co/models/{model}"
        headers = {
            "Authorization": f"Bearer {HF_TOKEN}",
            "Accept": "image/png"
        }
        payload = {
            "inputs": pos_prompt,
            "parameters": {
                "negative_prompt": neg_prompt,
                "num_inference_steps": 24,
                "guidance_scale": 7.0
            },
            "options": {"wait_for_model": True}
        }

        log.info("IMG CALL -> model=%r url=%r", model, url)
        resp = requests.post(url, headers=headers, json=payload, timeout=180)
        ct = resp.headers.get("content-type", "")
        log.info("IMG %s | %s", resp.status_code, ct)

        if resp.status_code == 200 and ct.startswith("image/"):
            bio = BytesIO(resp.content)
            bio.name = "image.png"
            shown_prompt = prompt_src if prompt_src else prompt_en
            message.reply_photo(bio, caption=f"🎨 По запросу: {shown_prompt}")
            return

        # дружелюбные сообщения по частым кодам
        if resp.status_code in (429, 503):
            message.reply_text("Модель занята или лимит. Попробуйте ещё раз через минуту ⏳")
            return

        snippet = (getattr(resp, "text", "") or "")[:800]
        message.reply_text(
            "❌ Hugging Face {code}\nМодель: {model}\nURL: {url}\n\n{snippet}".format(
                code=resp.status_code, model=model, url=url, snippet=snippet
            )
        )

    except Exception:
        traceback.print_exc()
        message.reply_text("Ошибка при генерации изображения 🎨")

# ----- ТЕКСТОВЫЙ ДИАЛОГ С ПАМЯТЬЮ (OpenRouter) -----
@app.on_message(filters.text & ~filters.command([
    "start","reset","img","catalog","find","sync1c","help","ping"
]))
def text_handler(_, message):
    uid = message.from_user.id
    user_text = (message.text or "").strip()

    # быстрые ответы на приветствия/ключевые фразы
    low = user_text.lower()
    if re.search(r"\b(привет|здравствуй|здравствуйте|добрый день|hi|hello)\b", low):
        message.reply_text("Привет! Чем помочь: каталог (/catalog) или поиск (/find <запрос>)?")
        return
    if "кабель" in low:
        message.reply_text("Ищешь кабель? Пример запроса: `/find кабель 35 мм`", quote=True)
        return
    if "пускател" in low:
        message.reply_text("По пускателям — `/find пускатель 95А 220В` или `/find пускатель 250А`", quote=True)
        return
    if "автомат" in low or re.search(r"\b\d{2,3}\s?а\b", low):
        message.reply_text("Нужен автомат? Попробуй: `/find автомат 400А` или `/find автомат 630А ABB`", quote=True)
        return

    # диалог с памятью (OpenRouter)
    chat_history[uid].append({"role": "user", "content": user_text})
    chat_history[uid] = clamp_history(chat_history[uid])

    try:
        payload = {
            "model": OR_MODEL,
            "messages": [
                {"role": "system", "content": "Ты — дружелюбный Telegram-бот магазина электрооборудования. Отвечай кратко и по делу."},
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
            txt = (getattr(resp, "text", "") or "")[:600]
            message.reply_text(f"❌ OpenRouter {resp.status_code}\n{txt}")
            return

        data = resp.json()
        bot_reply = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        bot_reply = bot_reply.strip() or "🤖 (пустой ответ)"

        chat_history[uid].append({"role": "assistant", "content": bot_reply})
        chat_history[uid] = clamp_history(chat_history[uid])

        message.reply_text(bot_reply)

    except Exception:
        traceback.print_exc()
        message.reply_text("Произошла ошибка при общении с OpenRouter 🤖")

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
        # предварительная загрузка каталога и запуск периодического обновления
        if CATALOG_URL:
            if not fetch_catalog(force=True):
                log.warning("Каталог не удалось загрузить на старте")
            periodic_refresh()
        app.run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
















